import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database import CollationDB, DomainError, split_units


class EmendationFlowTest(unittest.TestCase):
    def setUp(self):
        fd,self.path=tempfile.mkstemp(suffix=".db"); os.close(fd); self.db=CollationDB(self.path)
        self.owner=self.db.add_user("负责人","owner")
        self.editor=self.db.add_user("编辑","editor")
        self.reviewer=self.db.add_user("审阅","reviewer")
        self.other=self.db.add_user("其他审阅","reviewer")
        self.work=self.db.create_work("残卷","字位校勘",self.owner)
        self.w1=self.db.add_witness(self.work,"甲本","version")
        self.w2=self.db.add_witness(self.work,"乙本","fragment","馆藏残片","中段缺页")
        self.db.grant_witness_editor(self.w2,self.editor,self.owner)
        self.db.grant_work_access(self.work,self.reviewer,"review",self.owner)
        self.db.grant_work_access(self.work,self.other,"review",self.owner)
        self.passage=self.db.add_passage(self.work,"第一节","春水东流，故人南去。",self.owner)
        self.db.align_passage(self.passage,self.w1,"春水东流，故人南去。",1,self.owner)
        # 乙本：6、7 两字位不可辨，末句残损
        self.db.align_passage(self.passage,self.w2,"春水东流，[不可辨][不可辨]南去。[残损]",2,self.editor)

    def tearDown(self): self.db.close(); os.unlink(self.path)

    def test_split_units(self):
        self.assertEqual(["春","水","[不可辨]","[不可辨]","。"],split_units("春水[不可辨][不可辨]。"))

    def test_submit_registers_original_char_by_position(self):
        eid=self.db.submit_emendation(self.passage,self.w2,6,"故","参甲本用字",self.editor,0)
        rows=self.db.list_emendations(self.passage,self.w2,self.reviewer)
        self.assertEqual(1,len(rows))
        row=rows[0]
        self.assertEqual("[不可辨]",row["original_char"])
        self.assertEqual("故",row["proposed_char"])
        self.assertEqual("pending",row["status"])
        self.assertEqual(eid,row["id"])

    def test_one_open_slot_per_position(self):
        self.db.submit_emendation(self.passage,self.w2,6,"故","依据甲乙",self.editor,0)
        with self.assertRaisesRegex(DomainError,"已有"):
            self.db.submit_emendation(self.passage,self.w2,6,"古","另一说",self.editor,0)

    def test_gap_position_rejected(self):
        # 第11字位是 [残损]
        with self.assertRaisesRegex(DomainError,"缺口"):
            self.db.submit_emendation(self.passage,self.w2,11,"也","据残笔推补",self.editor,0)
        with self.assertRaisesRegex(DomainError,"超出范围"):
            self.db.submit_emendation(self.passage,self.w2,99,"也","越界补字",self.editor,0)

    def test_position_shift_blocks_approval(self):
        eid=self.db.submit_emendation(self.passage,self.w2,8,"南","原字待核",self.editor,0)
        # 通过新异文层之外的手段不好改对齐，这里直接改库模拟对齐层字位移动
        with self.db.transaction():
            self.db.conn.execute("UPDATE alignments SET aligned_text=? WHERE passage_id=? AND witness_id=?",
                                 ("春水东流，北[不可辨][不可辨]去。[残损]",self.passage,self.w2))
            self.db.conn.execute("UPDATE passages SET revision=revision+1 WHERE id=?",(self.passage,))
        with self.assertRaisesRegex(DomainError,"字位 8 已变化"):
            self.db.review_emendation(eid,"approve",self.reviewer,"",1)

    def test_self_review_forbidden(self):
        eid=self.db.submit_emendation(self.passage,self.w2,6,"故","参甲本",self.editor,0)
        with self.assertRaisesRegex(DomainError,"自己"):
            self.db.review_emendation(eid,"approve",self.editor,"",0)

    def test_reject_requires_reason(self):
        eid=self.db.submit_emendation(self.passage,self.w2,6,"故","参甲本",self.editor,0)
        with self.assertRaisesRegex(DomainError,"驳回必须写明原因"):
            self.db.review_emendation(eid,"reject",self.reviewer,"",0)

    def test_approve_becomes_current_reading(self):
        eid=self.db.submit_emendation(self.passage,self.w2,6,"故","参甲本",self.editor,0)
        self.db.review_emendation(eid,"approve",self.reviewer,"",0)
        exported=self.db.export_collation(self.work,self.reviewer)
        item=exported["passages"][0]["alignments"][1]
        self.assertEqual("春水东流，故[不可辨]南去。[残损]",item["reading"])
        pending=item["pending_positions"]
        self.assertEqual(0,len(pending))  # 只有一张校记且已通过
        self.assertEqual(0,exported["unresolved_count"])
        # 已通过字位不能再登记
        with self.assertRaisesRegex(DomainError,"已通过"):
            self.db.submit_emendation(self.passage,self.w2,6,"古","新说另据",self.editor,0)

    def test_reject_returns_with_reason(self):
        eid=self.db.submit_emendation(self.passage,self.w2,6,"故","参甲本",self.editor,0)
        self.db.review_emendation(eid,"reject",self.other,"甲本亦有讹误，不足据",0)
        row=self.db.list_emendations(self.passage,self.w2,self.reviewer)[0]
        self.assertEqual("rejected",row["status"])
        self.assertEqual("甲本亦有讹误，不足据",row["review_reason"])
        exported=self.db.export_collation(self.work,self.reviewer)
        self.assertEqual(1,exported["unresolved_count"])

    def test_variant_layer_change_invalidates_emendations(self):
        eid=self.db.submit_emendation(self.passage,self.w2,6,"故","参甲本",self.editor,0)
        # 异文层变化（修订号 0->1 由新异文产生），旧页面带 expected_revision=0 不能通过
        self.db.create_variant(self.passage,self.w2,"春水东流，故人南去。[残损]","按他本补足",self.editor,0)
        row=self.db.list_emendations(self.passage,self.w2,self.reviewer)[0]
        self.assertEqual("stale",row["status"])
        with self.assertRaisesRegex(DomainError,"不在待审队列"):
            self.db.review_emendation(eid,"approve",self.reviewer)
        # 重新核对字位后回到待审，再通过
        self.db.resubmit_emendation(eid,self.editor,1)
        self.db.review_emendation(eid,"approve",self.reviewer,"",1)
        row=self.db.list_emendations(self.passage,self.w2,self.reviewer)[0]
        self.assertEqual("approved",row["status"])

    def test_review_permission(self):
        eid=self.db.submit_emendation(self.passage,self.w2,6,"故","参甲本",self.editor,0)
        outsider=self.db.add_user("路人甲","reviewer")
        with self.assertRaisesRegex(DomainError,"无权审阅"):
            self.db.review_emendation(eid,"approve",outsider,"",0)

    def test_locked_passage_blocks_emendation(self):
        self.db.lock_passage(self.passage,self.owner,"定稿")
        with self.assertRaisesRegex(DomainError,"锁定"):
            self.db.submit_emendation(self.passage,self.w2,6,"故","参甲本",self.editor,0)

    def test_export_shows_reading_and_unresolved(self):
        self.db.submit_emendation(self.passage,self.w2,6,"故","参甲本",self.editor,0)
        exported=self.db.export_collation(self.work,self.reviewer)
        item=exported["passages"][0]["alignments"][1]
        # 未通过时释文仍为原对齐文本
        self.assertEqual("春水东流，[不可辨][不可辨]南去。[残损]",item["reading"])
        self.assertEqual(1,len(item["pending_positions"]))
        self.assertEqual(6,item["pending_positions"][0]["position"])
        self.assertEqual(1,exported["unresolved_count"])
        self.assertEqual(1,exported["gap_count"])


if __name__=="__main__": unittest.main()
