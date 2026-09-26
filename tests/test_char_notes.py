import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database import CollationDB, DomainError

class CharNoteFlowTest(unittest.TestCase):
    def setUp(self):
        fd,self.path=tempfile.mkstemp(suffix=".db"); os.close(fd); self.db=CollationDB(self.path)
        self.owner=self.db.add_user("负责人","owner"); self.editor=self.db.add_user("编辑","editor")
        self.reviewer=self.db.add_user("审阅","reviewer"); self.viewer=self.db.add_user("旁观","reviewer")
        self.work=self.db.create_work("残卷","字位校记",self.owner)
        self.w1=self.db.add_witness(self.work,"甲本","version")
        self.w2=self.db.add_witness(self.work,"乙本","fragment","馆藏残片","中段缺页")
        self.db.grant_witness_editor(self.w2,self.editor,self.owner)
        self.db.grant_work_access(self.work,self.reviewer,"review",self.owner)
        self.db.grant_work_access(self.work,self.viewer,"view",self.owner)
        self.passage=self.db.add_passage(self.work,"第一节","春水东流，故人南去。",self.owner)
        self.db.align_passage(self.passage,self.w1,"春水东流，故人南去。",1,self.owner)
        self.db.align_passage(self.passage,self.w2,"春水东流，[不可辨][不可辨]。",2,self.editor)
        self.variant=self.db.create_variant(self.passage,self.w2,"春水东流，故人南去。","据甲本补足",self.editor,0)
    def tearDown(self): self.db.close(); os.unlink(self.path)
    def _alignment(self,exported):
        return [a for a in exported["passages"][0]["alignments"] if a["witness_id"]==self.w2][0]

    def test_approve_writes_reading_and_export_shows_pending(self):
        note=self.db.add_char_note(self.variant,6,"[不可辨]","故","据甲本及文意补足",self.editor)
        exported=self.db.export_collation(self.work,self.reviewer)
        align=self._alignment(exported)
        self.assertEqual(1,exported["pending_note_count"])
        self.assertEqual(6,align["pending_positions"][0]["position"])
        self.assertEqual("故",align["pending_positions"][0]["proposed_char"])
        self.assertEqual("春水东流，[不可辨][不可辨]。",align["reading_text"])
        self.db.review_char_note(note,self.reviewer,"approve")
        align=self._alignment(self.db.export_collation(self.work,self.reviewer))
        self.assertEqual("春水东流，故[不可辨]。",align["reading_text"])
        self.assertEqual([{"position":6,"original_char":"[不可辨]","char":"故","note_id":note,"author":"编辑"}],align["approved_positions"])
        self.assertEqual(0,self.db.export_collation(self.work,self.reviewer)["pending_note_count"])

    def test_position_mismatch_gap_token_and_range_rejected(self):
        with self.assertRaisesRegex(DomainError,"字位可能已变化"):
            self.db.add_char_note(self.variant,4,"河","流","原字记错",self.editor)
        with self.assertRaisesRegex(DomainError,"超出"):
            self.db.add_char_note(self.variant,99,"流","逝","序号过大",self.editor)
        with self.assertRaisesRegex(DomainError,"确定文字"):
            self.db.add_char_note(self.variant,6,"[不可辨]","[缺页]","拟字不能是缺页",self.editor)
        p2=self.db.add_passage(self.work,"第二节","白日依山尽。",self.owner)
        self.db.align_passage(p2,self.w2,"白日依山尽，[缺页]",2,self.editor)
        v2=self.db.create_variant(p2,self.w2,"白日依山尽，黄河入海流。","据别本补",self.editor,0)
        with self.assertRaisesRegex(DomainError,"确定文字"):
            self.db.add_char_note(v2,7,"[缺页]","黄","缺页不能当确定文字",self.editor)

    def test_single_pending_per_position_across_variants(self):
        self.db.add_char_note(self.variant,6,"[不可辨]","故","依据一",self.editor)
        with self.assertRaisesRegex(DomainError,"待审"):
            self.db.add_char_note(self.variant,6,"[不可辨]","古","依据二",self.owner)
        other=self.db.create_variant(self.passage,self.w2,"春水东流，故国南去。","另一拟订",self.editor,1)
        with self.assertRaisesRegex(DomainError,"待审"):
            self.db.add_char_note(other,6,"[不可辨]","古","同一字位跨异文",self.editor)

    def test_reject_requires_reason_and_frees_position(self):
        note=self.db.add_char_note(self.variant,6,"[不可辨]","故","依据一",self.editor)
        with self.assertRaisesRegex(DomainError,"原因"):
            self.db.review_char_note(note,self.reviewer,"reject")
        self.db.review_char_note(note,self.reviewer,"reject","拟字缺少版本依据")
        again=self.db.add_char_note(self.variant,6,"[不可辨]","故","补充甲本照片后重报",self.editor)
        self.assertNotEqual(note,again)

    def test_reviewer_cannot_handle_own_note_and_needs_permission(self):
        note=self.db.add_char_note(self.variant,6,"[不可辨]","故","依据",self.editor)
        with self.assertRaisesRegex(DomainError,"自己"):
            self.db.review_char_note(note,self.editor,"approve")
        with self.assertRaisesRegex(DomainError,"无权"):
            self.db.review_char_note(note,self.viewer,"approve")
        own=self.db.add_char_note(self.variant,7,"[不可辨]","人","依据",self.owner)
        with self.assertRaisesRegex(DomainError,"自己"):
            self.db.review_char_note(own,self.owner,"approve")
        with self.assertRaisesRegex(DomainError,"已处理"):
            self.db.review_char_note(999,self.reviewer,"approve")

    def test_layer_change_stales_notes_and_requires_rereview(self):
        approved=self.db.add_char_note(self.variant,6,"[不可辨]","故","依据一",self.editor)
        self.db.review_char_note(approved,self.reviewer,"approve")
        pending=self.db.add_char_note(self.variant,7,"[不可辨]","人","依据二",self.editor)
        self.db.update_variant(self.variant,"春水东流，故友南去。","改订拟文",self.editor,1)
        notes={n["id"]:n for n in self.db.list_char_notes(self.passage,self.owner)}
        self.assertEqual("stale",notes[approved]["status"])
        self.assertEqual("stale",notes[pending]["status"])
        align=self._alignment(self.db.export_collation(self.work,self.owner))
        self.assertEqual("春水东流，[不可辨][不可辨]。",align["reading_text"])
        self.assertEqual(0,self.db.export_collation(self.work,self.owner)["pending_note_count"])
        with self.assertRaisesRegex(DomainError,"已处理"):
            self.db.review_char_note(pending,self.reviewer,"approve")
        again=self.db.add_char_note(self.variant,6,"[不可辨]","故","层变后重新登记",self.editor)
        self.db.review_char_note(again,self.reviewer,"approve")
        align=self._alignment(self.db.export_collation(self.work,self.owner))
        self.assertEqual("春水东流，故[不可辨]。",align["reading_text"])

    def test_locked_passage_rejects_new_notes(self):
        self.db.lock_passage(self.passage,self.owner,"定稿")
        with self.assertRaisesRegex(DomainError,"锁定"):
            self.db.add_char_note(self.variant,6,"[不可辨]","故","依据",self.editor)

if __name__=="__main__": unittest.main()
