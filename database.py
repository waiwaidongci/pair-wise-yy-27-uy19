from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class DomainError(ValueError):
    """Business rule violation."""


WITNESS_KINDS = {"version", "fragment", "transcription"}
SPECIAL_TOKENS = {"[缺页]", "[不可辨]", "[残损]", "[插入]", "[删除]"}
# 缺页、残损属于实物缺口，不能在字位校记里当作确定文字释读
GAP_TOKENS = {"[缺页]", "[残损]"}
EMENDATION_STATUSES = ("pending", "approved", "rejected", "stale")


def split_units(text: str) -> list[str]:
    """把对齐文本拆成字位序号：普通字符各占一位，[标记] 整体占一位。"""
    units: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "[":
            j = text.find("]", i + 1)
            if j == -1:  # 容错：validate_transcription 已保证括号配对
                units.append(text[i])
                i += 1
            else:
                units.append(text[i:j + 1])
                i = j + 1
        else:
            units.append(text[i])
            i += 1
    return units


def validate_transcription(text: str) -> str:
    text = text.strip()
    if not text:
        raise DomainError("文本不能为空")
    unclosed = text.count("[") - text.count("]")
    if unclosed:
        raise DomainError("校勘标记括号不匹配")
    return text


class CollationDB:
    """SQLite-backed textual collation service with optimistic revisions."""

    def __init__(self, path: str = "collation.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              role TEXT NOT NULL CHECK(role IN ('owner','editor','reviewer'))
            );
            CREATE TABLE IF NOT EXISTS works (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              owner_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_access (
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id),
              permission TEXT NOT NULL CHECK(permission IN ('view','review')),
              PRIMARY KEY(work_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS witnesses (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              siglum TEXT NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('version','fragment','transcription')),
              source_note TEXT NOT NULL DEFAULT '',
              missing_sections TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              UNIQUE(work_id,siglum)
            );
            CREATE TABLE IF NOT EXISTS witness_editors (
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id),
              granted_by INTEGER NOT NULL REFERENCES users(id),
              PRIMARY KEY(witness_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS passages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              label TEXT NOT NULL,
              base_text TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','locked')),
              revision INTEGER NOT NULL DEFAULT 0,
              updated_by INTEGER NOT NULL REFERENCES users(id),
              updated_at TEXT NOT NULL,
              UNIQUE(work_id,label)
            );
            CREATE TABLE IF NOT EXISTS alignments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              aligned_text TEXT NOT NULL,
              sort_order INTEGER NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(passage_id,witness_id)
            );
            CREATE TABLE IF NOT EXISTS variants (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id),
              base_text TEXT NOT NULL,
              proposed_text TEXT NOT NULL,
              reason TEXT NOT NULL,
              layer INTEGER NOT NULL DEFAULT 1,
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              variant_id INTEGER REFERENCES variants(id) ON DELETE CASCADE,
              revision_no INTEGER NOT NULL,
              layer INTEGER NOT NULL,
              snapshot_json TEXT NOT NULL,
              author_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(passage_id, revision_no)
            );
            CREATE TABLE IF NOT EXISTS notes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              variant_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
              body TEXT NOT NULL,
              author_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS passage_locks (
              passage_id INTEGER PRIMARY KEY REFERENCES passages(id) ON DELETE CASCADE,
              locked_by INTEGER NOT NULL REFERENCES users(id),
              reason TEXT NOT NULL DEFAULT '',
              locked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS emendations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              position INTEGER NOT NULL,
              original_char TEXT NOT NULL,
              proposed_char TEXT NOT NULL,
              basis TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','stale')),
              base_revision INTEGER NOT NULL,
              created_by INTEGER NOT NULL REFERENCES users(id),
              reviewed_by INTEGER REFERENCES users(id),
              review_reason TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              reviewed_at TEXT,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_emendations_alignment ON emendations(passage_id,witness_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_emendations_open_slot
              ON emendations(passage_id,witness_id,position)
              WHERE status IN ('pending','approved');
            """
        )
        self.conn.commit()

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        owner = self.add_user("项目负责人", "owner")
        editor = self.add_user("校勘编辑", "editor")
        reviewer = self.add_user("审阅人", "reviewer")
        work = self.create_work("一则残卷", "演示不同版本的校勘", owner)
        w1 = self.add_witness(work, "甲本", "version", "馆藏胶片", "")
        w2 = self.add_witness(work, "乙本", "fragment", "残片转录", "第二句残损")
        self.grant_witness_editor(w2, editor, owner)
        self.grant_work_access(work, reviewer, "review", owner)
        passage = self.add_passage(work, "第1节", "春水东流，故人南去。", owner)
        self.align_passage(passage, w1, "春水东流，故人南去。", 1, owner)
        self.align_passage(passage, w2, "春水东流，[不可辨][不可辨]。", 2, owner)
        variant = self.create_variant(passage, w2, "春水东流，故人南去。", "综合语义与行款补足", owner, 0)
        self.add_note(variant, "补字仍需参照纸背墨迹。", editor)
        # 乙本第6字位为[不可辨]，编辑登记拟字“故”待审阅人审定
        self.submit_emendation(passage, w2, 6, "故", "参甲本用字与上下句文义", editor, 1)

    def add_user(self, name: str, role: str) -> int:
        if not name.strip() or role not in {"owner", "editor", "reviewer"}:
            raise DomainError("用户名或角色无效")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO users(name,role) VALUES(?,?)", (name.strip(), role))
            except sqlite3.IntegrityError as exc:
                raise DomainError("用户名已存在") from exc
        return int(cur.lastrowid)

    def create_work(self, title: str, description: str, owner_id: int) -> int:
        owner = self.conn.execute("SELECT role FROM users WHERE id=?", (owner_id,)).fetchone()
        if not owner or owner["role"] != "owner" or not title.strip():
            raise DomainError("作品标题或负责人无效")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO works(title,description,owner_id,created_at) VALUES(?,?,?,?)",
                (title.strip(), description.strip(), owner_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def grant_work_access(self, work_id: int, user_id: int, permission: str, granted_by: int) -> None:
        if permission not in {"view", "review"}:
            raise DomainError("权限必须为 view 或 review")
        self._require_owner(work_id, granted_by)
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            self.conn.execute(
                "INSERT INTO work_access(work_id,user_id,permission) VALUES(?,?,?) "
                "ON CONFLICT(work_id,user_id) DO UPDATE SET permission=excluded.permission",
                (work_id, user_id, permission),
            )

    def _require_owner(self, work_id: int, user_id: int) -> None:
        row = self.conn.execute("SELECT 1 FROM works WHERE id=? AND owner_id=?", (work_id, user_id)).fetchone()
        if not row:
            raise DomainError("只有项目负责人可以执行此操作")

    def can_view_work(self, work_id: int, user_id: int) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM works WHERE id=? AND owner_id=? "
            "UNION ALL SELECT 1 FROM work_access WHERE work_id=? AND user_id=? "
            "UNION ALL SELECT 1 FROM witnesses w JOIN witness_editors e ON e.witness_id=w.id "
            "WHERE w.work_id=? AND e.user_id=? LIMIT 1",
            (work_id, user_id, work_id, user_id, work_id, user_id),
        ).fetchone())

    def can_edit_witness(self, witness_id: int, user_id: int) -> bool:
        row = self.conn.execute(
            "SELECT w.work_id,wa.permission FROM witnesses w LEFT JOIN work_access wa ON wa.work_id=w.work_id AND wa.user_id=? WHERE w.id=?",
            (user_id, witness_id),
        ).fetchone()
        if not row:
            return False
        owner = self.conn.execute("SELECT 1 FROM works WHERE id=? AND owner_id=?", (row["work_id"], user_id)).fetchone()
        editor = self.conn.execute("SELECT 1 FROM witness_editors WHERE witness_id=? AND user_id=?", (witness_id, user_id)).fetchone()
        return bool(owner or editor)

    def add_witness(self, work_id: int, siglum: str, kind: str, source_note: str = "", missing_sections: str = "") -> int:
        if not self.conn.execute("SELECT 1 FROM works WHERE id=?", (work_id,)).fetchone():
            raise DomainError("作品不存在")
        if not siglum.strip() or kind not in WITNESS_KINDS:
            raise DomainError("版本标识或类型无效")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO witnesses(work_id,siglum,kind,source_note,missing_sections,created_at) VALUES(?,?,?,?,?,?)",
                    (work_id, siglum.strip(), kind, source_note.strip(), missing_sections.strip(), datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("同一作品中的版本标识不能重复") from exc
        return int(cur.lastrowid)

    def grant_witness_editor(self, witness_id: int, user_id: int, granted_by: int) -> None:
        witness = self.conn.execute("SELECT work_id FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not witness:
            raise DomainError("版本不存在")
        self._require_owner(witness["work_id"], granted_by)
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO witness_editors(witness_id,user_id,granted_by) VALUES(?,?,?)",
                (witness_id, user_id, granted_by),
            )

    def add_passage(self, work_id: int, label: str, base_text: str, user_id: int) -> int:
        self._require_owner(work_id, user_id)
        text = validate_transcription(base_text)
        if not label.strip():
            raise DomainError("段落标签不能为空")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO passages(work_id,label,base_text,updated_by,updated_at) VALUES(?,?,?,?,?)",
                    (work_id, label.strip(), text, user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("段落标签已存在") from exc
        return int(cur.lastrowid)

    def align_passage(self, passage_id: int, witness_id: int, aligned_text: str, sort_order: int, user_id: int) -> int:
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        witness = self.conn.execute("SELECT * FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not passage or not witness or passage["work_id"] != witness["work_id"]:
            raise DomainError("段落与版本不属于同一作品")
        if not self.can_edit_witness(witness_id, user_id):
            raise DomainError("无权编辑该版本")
        if sort_order <= 0:
            raise DomainError("排序号必须大于0")
        text = validate_transcription(aligned_text)
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO alignments(passage_id,witness_id,aligned_text,sort_order,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (passage_id, witness_id, text, sort_order, user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该版本已经对齐此段落") from exc
        return int(cur.lastrowid)

    def create_variant(self, passage_id: int, witness_id: int, proposed_text: str, reason: str,
                       user_id: int, expected_revision: int) -> int:
        with self.transaction():
            passage, lock = self._editable_passage(passage_id, witness_id, user_id, expected_revision)
            text = validate_transcription(proposed_text)
            if len(reason.strip()) < 3:
                raise DomainError("取舍理由至少3个字符")
            if not self.conn.execute("SELECT 1 FROM alignments WHERE passage_id=? AND witness_id=?", (passage_id, witness_id)).fetchone():
                raise DomainError("该版本尚未对齐此段落")
            cur = self.conn.execute(
                "INSERT INTO variants(passage_id,witness_id,base_text,proposed_text,reason,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (passage_id, witness_id, passage["base_text"], text, reason.strip(), user_id, datetime.now().isoformat(), datetime.now().isoformat()),
            )
            variant_id = int(cur.lastrowid)
            revision = self._record_revision(passage_id, variant_id, 1, user_id)
            self.conn.execute("UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?", (revision, user_id, datetime.now().isoformat(), passage_id))
            self._invalidate_emendations(passage_id, revision)
        return variant_id

    def update_variant(self, variant_id: int, proposed_text: str, reason: str, user_id: int,
                       expected_revision: int) -> int:
        with self.transaction():
            variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
            if not variant:
                raise DomainError("异文记录不存在")
            passage, _ = self._editable_passage(variant["passage_id"], variant["witness_id"], user_id, expected_revision)
            text = validate_transcription(proposed_text)
            if len(reason.strip()) < 3:
                raise DomainError("取舍理由至少3个字符")
            layer = int(self.conn.execute("SELECT COALESCE(MAX(layer),0)+1 FROM variants WHERE passage_id=? AND witness_id=?", (variant["passage_id"], variant["witness_id"])).fetchone()[0])
            self.conn.execute(
                "UPDATE variants SET proposed_text=?,reason=?,layer=?,updated_at=? WHERE id=?",
                (text, reason.strip(), layer, datetime.now().isoformat(), variant_id),
            )
            revision = self._record_revision(variant["passage_id"], variant_id, layer, user_id)
            self.conn.execute("UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?", (revision, user_id, datetime.now().isoformat(), variant["passage_id"]))
            self._invalidate_emendations(variant["passage_id"], revision)
        return revision

    def _invalidate_emendations(self, passage_id: int, revision: int) -> None:
        """异文层变化后，待审与已通过的字位校记一律失效，需要重新登记重审。"""
        self.conn.execute(
            "UPDATE emendations SET status='stale',base_revision=?,updated_at=? "
            "WHERE passage_id=? AND status IN ('pending','approved')",
            (revision, datetime.now().isoformat(), passage_id),
        )

    def _editable_passage(self, passage_id: int, witness_id: int, user_id: int, expected_revision: int):
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        witness = self.conn.execute("SELECT * FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not passage or not witness or passage["work_id"] != witness["work_id"]:
            raise DomainError("段落与版本不属于同一作品")
        if passage["status"] == "locked" or self.conn.execute("SELECT 1 FROM passage_locks WHERE passage_id=?", (passage_id,)).fetchone():
            raise DomainError("段落已锁定，不能修改")
        if not self.can_edit_witness(witness_id, user_id):
            raise DomainError("无权编辑该版本")
        if passage["revision"] != expected_revision:
            raise DomainError(f"版本冲突：当前修订为 {passage['revision']}，提交基于 {expected_revision}")
        return passage, None

    def _record_revision(self, passage_id: int, variant_id: int, layer: int, user_id: int) -> int:
        revision = int(self.conn.execute("SELECT COALESCE(MAX(revision_no),0)+1 FROM revisions WHERE passage_id=?", (passage_id,)).fetchone()[0])
        snapshot = {
            "passage": dict(self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()),
            "variant": dict(self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()),
            "alignments": [dict(r) for r in self.conn.execute(
                "SELECT a.*,w.siglum,w.kind FROM alignments a JOIN witnesses w ON w.id=a.witness_id WHERE a.passage_id=? ORDER BY a.sort_order",
                (passage_id,),
            ).fetchall()],
            "emendations": [dict(r) for r in self.conn.execute(
                "SELECT * FROM emendations WHERE passage_id=? ORDER BY witness_id,position,id",
                (passage_id,),
            ).fetchall()],
        }
        self.conn.execute(
            "INSERT INTO revisions(passage_id,variant_id,revision_no,layer,snapshot_json,author_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (passage_id, variant_id, revision, layer, json.dumps(snapshot, ensure_ascii=False), user_id, datetime.now().isoformat()),
        )
        return revision

    def add_note(self, variant_id: int, body: str, author_id: int) -> int:
        variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
        if not variant or not self.can_view_work(
            self.conn.execute("SELECT work_id FROM passages WHERE id=?", (variant["passage_id"],)).fetchone()["work_id"], author_id
        ):
            raise DomainError("异文不存在或无权评论")
        if not body.strip():
            raise DomainError("注释不能为空")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO notes(variant_id,body,author_id,created_at) VALUES(?,?,?,?)",
                (variant_id, body.strip(), author_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def lock_passage(self, passage_id: int, user_id: int, reason: str = "") -> None:
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage:
            raise DomainError("段落不存在")
        self._require_owner(passage["work_id"], user_id)
        with self.transaction():
            self.conn.execute("UPDATE passages SET status='locked',updated_by=?,updated_at=? WHERE id=?", (user_id, datetime.now().isoformat(), passage_id))
            self.conn.execute(
                "INSERT OR REPLACE INTO passage_locks(passage_id,locked_by,reason,locked_at) VALUES(?,?,?,?)",
                (passage_id, user_id, reason.strip(), datetime.now().isoformat()),
            )

    # ---- 字位校记 ----

    def _emendation_target(self, passage_id: int, witness_id: int, user_id: int, expected_revision: int):
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        witness = self.conn.execute("SELECT * FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not passage or not witness or passage["work_id"] != witness["work_id"]:
            raise DomainError("段落与版本不属于同一作品")
        if passage["status"] == "locked" or self.conn.execute("SELECT 1 FROM passage_locks WHERE passage_id=?", (passage_id,)).fetchone():
            raise DomainError("段落已锁定，不能登记校记")
        if not self.can_edit_witness(witness_id, user_id):
            raise DomainError("无权编辑该版本")
        alignment = self.conn.execute(
            "SELECT * FROM alignments WHERE passage_id=? AND witness_id=?", (passage_id, witness_id)
        ).fetchone()
        if not alignment:
            raise DomainError("该版本尚未对齐此段落")
        if passage["revision"] != expected_revision:
            raise DomainError(f"版本冲突：当前修订为 {passage['revision']}，提交基于 {expected_revision}")
        return passage, witness, alignment

    @staticmethod
    def _unit_at(aligned_text: str, position: int) -> str:
        units = split_units(aligned_text)
        if position < 1 or position > len(units):
            raise DomainError(f"字位序号超出范围（1-{len(units)}）")
        return units[position - 1]

    def submit_emendation(self, passage_id: int, witness_id: int, position: int,
                          proposed_char: str, basis: str, user_id: int, expected_revision: int) -> int:
        """按对齐文本序号登记字位校记：原字取自当前对齐层，一个字位只留一张待审单。"""
        try:
            position = int(position)
        except (TypeError, ValueError):
            raise DomainError("字位序号必须是整数")
        proposed_char = str(proposed_char or "").strip()
        basis = str(basis or "").strip()
        if len(proposed_char) != 1:
            raise DomainError("拟字必须是单个字位")
        if len(basis) < 3:
            raise DomainError("校勘依据至少3个字符")
        with self.transaction():
            passage, _, alignment = self._emendation_target(passage_id, witness_id, user_id, expected_revision)
            original = self._unit_at(alignment["aligned_text"], position)
            if original in GAP_TOKENS:
                raise DomainError(f"字位 {position} 为{original}缺口，不能当作确定文字释读")
            open_slot = self.conn.execute(
                "SELECT id,status FROM emendations WHERE passage_id=? AND witness_id=? AND position=? "
                "AND status IN ('pending','approved')",
                (passage_id, witness_id, position),
            ).fetchone()
            if open_slot:
                raise DomainError(f"字位 {position} 已有{ '待审' if open_slot['status']=='pending' else '已通过' }校记，不能重复登记")
            now = datetime.now().isoformat()
            cur = self.conn.execute(
                "INSERT INTO emendations(passage_id,witness_id,position,original_char,proposed_char,basis,"
                "status,base_revision,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (passage_id, witness_id, position, original, proposed_char, basis,
                 "pending", passage["revision"], user_id, now, now),
            )
        return int(cur.lastrowid)

    def _can_review_work(self, work_id: int, user_id: int) -> bool:
        if self.conn.execute("SELECT 1 FROM works WHERE id=? AND owner_id=?", (work_id, user_id)).fetchone():
            return True
        return bool(self.conn.execute(
            "SELECT 1 FROM work_access WHERE work_id=? AND user_id=? AND permission='review'",
            (work_id, user_id),
        ).fetchone())

    def review_emendation(self, emendation_id: int, action: str, reviewer_id: int,
                          reason: str = "", expected_revision: int | None = None) -> None:
        if action not in {"approve", "reject"}:
            raise DomainError("审定动作必须为 approve 或 reject")
        reason = str(reason or "").strip()
        if action == "reject" and not reason:
            raise DomainError("驳回必须写明原因")
        with self.transaction():
            row = self.conn.execute("SELECT * FROM emendations WHERE id=?", (emendation_id,)).fetchone()
            if not row:
                raise DomainError("校记不存在")
            passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (row["passage_id"],)).fetchone()
            witness = self.conn.execute("SELECT work_id FROM witnesses WHERE id=?", (row["witness_id"],)).fetchone()
            if not passage or not witness:
                raise DomainError("校记所属段落或版本不存在")
            if row["created_by"] == reviewer_id:
                raise DomainError("审阅人不能处理自己登记的校记")
            if not self._can_review_work(passage["work_id"], reviewer_id):
                raise DomainError("无权审阅该校记")
            if passage["status"] == "locked" or self.conn.execute("SELECT 1 FROM passage_locks WHERE passage_id=?", (row["passage_id"],)).fetchone():
                raise DomainError("段落已锁定，不能审定校记")
            if expected_revision is not None and passage["revision"] != int(expected_revision):
                raise DomainError(f"版本冲突：当前修订为 {passage['revision']}，提交基于 {expected_revision}")
            if row["status"] != "pending":
                raise DomainError(f"校记当前状态为 {row['status']}，不在待审队列")
            # 审定时再次核对字位：对齐层改动导致原字移位/改变则拒绝
            alignment = self.conn.execute(
                "SELECT * FROM alignments WHERE passage_id=? AND witness_id=?",
                (row["passage_id"], row["witness_id"]),
            ).fetchone()
            current = self._unit_at(alignment["aligned_text"], row["position"])
            if current != row["original_char"]:
                raise DomainError(
                    f"字位 {row['position']} 已变化（登记原字“{row['original_char']}”，当前为“{current}”），请重新登记"
                )
            if current in GAP_TOKENS:
                raise DomainError(f"字位 {row['position']} 已成为缺口，不能当作确定文字释读")
            if row["base_revision"] != passage["revision"]:
                raise DomainError("异文层已变化，校记失效，需要重审")
            now = datetime.now().isoformat()
            if action == "approve":
                self.conn.execute(
                    "UPDATE emendations SET status='approved',reviewed_by=?,review_reason='',reviewed_at=?,updated_at=? WHERE id=?",
                    (reviewer_id, now, now, emendation_id),
                )
            else:
                self.conn.execute(
                    "UPDATE emendations SET status='rejected',reviewed_by=?,review_reason=?,reviewed_at=?,updated_at=? WHERE id=?",
                    (reviewer_id, reason, now, now, emendation_id),
                )

    def resubmit_emendation(self, emendation_id: int, user_id: int, expected_revision: int) -> int:
        """失效或被驳回的校记，按当前对齐层重新核对后回到待审队列；拟字与依据沿用原单。"""
        with self.transaction():
            row = self.conn.execute("SELECT * FROM emendations WHERE id=?", (emendation_id,)).fetchone()
            if not row:
                raise DomainError("校记不存在")
            passage, _, alignment = self._emendation_target(
                row["passage_id"], row["witness_id"], user_id, expected_revision
            )
            if row["status"] not in {"stale", "rejected"}:
                raise DomainError(f"校记当前状态为 {row['status']}，不能重新提交")
            current = self._unit_at(alignment["aligned_text"], row["position"])
            if current in GAP_TOKENS:
                raise DomainError(f"字位 {row['position']} 为缺口，不能当作确定文字释读")
            blocked = self.conn.execute(
                "SELECT id FROM emendations WHERE passage_id=? AND witness_id=? AND position=? "
                "AND status IN ('pending','approved') AND id<>?",
                (row["passage_id"], row["witness_id"], row["position"], emendation_id),
            ).fetchone()
            if blocked:
                raise DomainError(f"字位 {row['position']} 已有其他待审校记")
            self.conn.execute(
                "UPDATE emendations SET status='pending',original_char=?,base_revision=?,"
                "reviewed_by=NULL,review_reason='',reviewed_at=NULL,updated_at=? WHERE id=?",
                (current, passage["revision"], datetime.now().isoformat(), emendation_id),
            )
        return emendation_id

    def list_emendations(self, passage_id: int, witness_id: int, user_id: int) -> list[dict]:
        passage = self.conn.execute("SELECT work_id FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage or not self.can_view_work(passage["work_id"], user_id):
            raise DomainError("无权查看该校记")
        rows = self.conn.execute(
            "SELECT e.*,cu.name AS created_name,ru.name AS reviewed_name FROM emendations e "
            "JOIN users cu ON cu.id=e.created_by LEFT JOIN users ru ON ru.id=e.reviewed_by "
            "WHERE e.passage_id=? AND e.witness_id=? ORDER BY e.position,e.id",
            (passage_id, witness_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_snapshot(self, passage_id: int, revision_no: int, user_id: int) -> dict:
        passage = self.conn.execute("SELECT work_id FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage or not self.can_view_work(passage["work_id"], user_id):
            raise DomainError("无权查看该快照")
        row = self.conn.execute("SELECT * FROM revisions WHERE passage_id=? AND revision_no=?", (passage_id, revision_no)).fetchone()
        if not row:
            raise DomainError("快照不存在")
        return {"revision_no": row["revision_no"], "layer": row["layer"], "created_at": row["created_at"], "snapshot": json.loads(row["snapshot_json"])}

    def export_collation(self, work_id: int, user_id: int) -> dict:
        if not self.can_view_work(work_id, user_id):
            raise DomainError("无权查看该校勘项目")
        work = self.conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        witnesses = [dict(r) for r in self.conn.execute("SELECT * FROM witnesses WHERE work_id=? ORDER BY id", (work_id,))]
        passages = []
        gaps = 0
        unresolved = 0
        for passage in self.conn.execute("SELECT * FROM passages WHERE work_id=? ORDER BY id", (work_id,)).fetchall():
            alignments = []
            for row in self.conn.execute(
                "SELECT a.*,w.siglum,w.kind,w.missing_sections FROM alignments a JOIN witnesses w ON w.id=a.witness_id "
                "WHERE a.passage_id=? ORDER BY a.sort_order", (passage["id"],)
            ).fetchall():
                item = dict(row)
                if "[缺页]" in item["aligned_text"] or "[残损]" in item["aligned_text"]:
                    item["has_gap"] = True
                    gaps += 1
                em_rows = self.conn.execute(
                    "SELECT e.*,cu.name AS created_name,ru.name AS reviewed_name FROM emendations e "
                    "JOIN users cu ON cu.id=e.created_by LEFT JOIN users ru ON ru.id=e.reviewed_by "
                    "WHERE e.passage_id=? AND e.witness_id=? ORDER BY e.position,e.id",
                    (passage["id"], row["witness_id"]),
                ).fetchall()
                emendations = [dict(r) for r in em_rows]
                item["emendations"] = emendations
                # 当前释文：对齐文本上叠加已通过校记；未决字位（待审/失效/被驳回）单列
                units = split_units(item["aligned_text"])
                approved: dict[int, str] = {}
                pending_positions = []
                for em in emendations:
                    if em["status"] == "approved":
                        approved[em["position"]] = em["proposed_char"]
                    else:
                        if 1 <= em["position"] <= len(units):
                            pending_positions.append({
                                "position": em["position"],
                                "original_char": units[em["position"] - 1],
                                "proposed_char": em["proposed_char"],
                                "status": em["status"],
                                "basis": em["basis"],
                            })
                        unresolved += 1
                reading = "".join(approved.get(i + 1, ch) for i, ch in enumerate(units))
                item["reading"] = reading
                item["pending_positions"] = pending_positions
                alignments.append(item)
            variants = []
            for row in self.conn.execute("SELECT * FROM variants WHERE passage_id=? ORDER BY witness_id,layer,id", (passage["id"],)).fetchall():
                variant = dict(row)
                variant["notes"] = [dict(r) for r in self.conn.execute("SELECT * FROM notes WHERE variant_id=? ORDER BY id", (row["id"],))]
                variants.append(variant)
            passages.append({**dict(passage), "alignments": alignments, "variants": variants})
        return {"work": dict(work), "witnesses": witnesses, "passages": passages,
                "gap_count": gaps, "unresolved_count": unresolved}

    def snapshot(self) -> dict:
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "works": [dict(r) for r in self.conn.execute("SELECT * FROM works ORDER BY id")],
            "witnesses": [dict(r) for r in self.conn.execute("SELECT * FROM witnesses ORDER BY id")],
            "passages": [dict(r) for r in self.conn.execute("SELECT * FROM passages ORDER BY id")],
        }
