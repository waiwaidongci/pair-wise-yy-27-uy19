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


def validate_transcription(text: str) -> str:
    text = text.strip()
    if not text:
        raise DomainError("文本不能为空")
    unclosed = text.count("[") - text.count("]")
    if unclosed:
        raise DomainError("校勘标记括号不匹配")
    return text


def tokenize_aligned(text: str) -> list:
    """Split aligned text into character positions; each [...] marker is one position."""
    tokens, i = [], 0
    while i < len(text):
        if text[i] == "[":
            j = text.find("]", i)
            if j == -1:
                raise DomainError("校勘标记括号不匹配")
            tokens.append(text[i:j + 1])
            i = j + 1
        else:
            tokens.append(text[i])
            i += 1
    return tokens


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
            CREATE TABLE IF NOT EXISTS char_notes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              variant_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
              layer INTEGER NOT NULL,
              position INTEGER NOT NULL,
              original_char TEXT NOT NULL,
              proposed_char TEXT NOT NULL,
              evidence TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','stale')),
              author_id INTEGER NOT NULL REFERENCES users(id),
              reviewer_id INTEGER REFERENCES users(id),
              review_reason TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              reviewed_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS char_notes_one_pending
              ON char_notes(passage_id,witness_id,position) WHERE status='pending';
            CREATE TABLE IF NOT EXISTS lectiones (
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              position INTEGER NOT NULL,
              char TEXT NOT NULL,
              note_id INTEGER NOT NULL REFERENCES char_notes(id) ON DELETE CASCADE,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(passage_id,witness_id,position)
            );
            """
        )
        self.conn.commit()

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        owner = self.add_user("项目负责人", "owner")
        editor = self.add_user("校勘编辑", "editor")
        work = self.create_work("一则残卷", "演示不同版本的校勘", owner)
        w1 = self.add_witness(work, "甲本", "version", "馆藏胶片", "")
        w2 = self.add_witness(work, "乙本", "fragment", "残片转录", "第二句残损")
        self.grant_witness_editor(w2, editor, owner)
        passage = self.add_passage(work, "第1节", "春水东流，故人南去。", owner)
        self.align_passage(passage, w1, "春水东流，故人南去。", 1, owner)
        self.align_passage(passage, w2, "春水东流，[不可辨][不可辨]。", 2, owner)
        variant = self.create_variant(passage, w2, "春水东流，故人南去。", "综合语义与行款补足", owner, 0)
        self.add_note(variant, "补字仍需参照纸背墨迹。", editor)
        reviewer = self.add_user("审阅专家", "reviewer")
        self.grant_work_access(work, reviewer, "review", owner)
        note = self.add_char_note(variant, 6, "[不可辨]", "故", "甲本作故，文意亦通", editor)
        self.review_char_note(note, reviewer, "approve")
        self.add_char_note(variant, 7, "[不可辨]", "人", "残画存人字右半", editor)

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

    def can_review_work(self, work_id: int, user_id: int) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM works WHERE id=? AND owner_id=? "
            "UNION ALL SELECT 1 FROM work_access WHERE work_id=? AND user_id=? AND permission='review' LIMIT 1",
            (work_id, user_id, work_id, user_id),
        ).fetchone())

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
            self._stale_variant_notes(variant_id)
            revision = self._record_revision(variant["passage_id"], variant_id, layer, user_id)
            self.conn.execute("UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?", (revision, user_id, datetime.now().isoformat(), variant["passage_id"]))
        return revision

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

    def add_char_note(self, variant_id: int, position: int, original_char: str, proposed_char: str,
                      evidence: str, user_id: int) -> int:
        variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
        if not variant:
            raise DomainError("异文记录不存在")
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (variant["passage_id"],)).fetchone()
        if passage["status"] == "locked" or self.conn.execute("SELECT 1 FROM passage_locks WHERE passage_id=?", (passage["id"],)).fetchone():
            raise DomainError("段落已锁定，不能登记校记")
        if not self.can_edit_witness(variant["witness_id"], user_id):
            raise DomainError("无权编辑该版本")
        alignment = self.conn.execute(
            "SELECT aligned_text FROM alignments WHERE passage_id=? AND witness_id=?",
            (variant["passage_id"], variant["witness_id"]),
        ).fetchone()
        if not alignment:
            raise DomainError("该版本尚未对齐此段落")
        tokens = tokenize_aligned(alignment["aligned_text"])
        if position < 1 or position > len(tokens):
            raise DomainError("字位超出对齐文本范围")
        token = tokens[position - 1]
        if token.startswith("[") and token != "[不可辨]":
            raise DomainError("缺页、残损等标记不能当作确定文字登记")
        if original_char.strip() != token:
            raise DomainError("原字与当前对齐文本不符，字位可能已变化")
        proposed = proposed_char.strip()
        if proposed in SPECIAL_TOKENS or "[" in proposed or "]" in proposed:
            raise DomainError("缺页、残损等标记不能当作确定文字登记")
        if len(proposed) != 1:
            raise DomainError("拟字必须是单个字符")
        if not evidence.strip():
            raise DomainError("依据不能为空")
        with self.transaction():
            if self.conn.execute(
                "SELECT 1 FROM char_notes WHERE passage_id=? AND witness_id=? AND position=? AND status='pending'",
                (variant["passage_id"], variant["witness_id"], position),
            ).fetchone():
                raise DomainError("该字位已存在待审校记")
            cur = self.conn.execute(
                "INSERT INTO char_notes(passage_id,witness_id,variant_id,layer,position,original_char,proposed_char,evidence,author_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (variant["passage_id"], variant["witness_id"], variant_id, variant["layer"], position,
                 token, proposed, evidence.strip(), user_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def review_char_note(self, note_id: int, user_id: int, action: str, reason: str = "") -> None:
        if action not in {"approve", "reject"}:
            raise DomainError("审阅动作必须为 approve 或 reject")
        with self.transaction():
            note = self.conn.execute("SELECT * FROM char_notes WHERE id=?", (note_id,)).fetchone()
            if not note or note["status"] != "pending":
                raise DomainError("校记不存在或已处理")
            if note["author_id"] == user_id:
                raise DomainError("不能审阅自己提交的校记")
            work_id = self.conn.execute("SELECT work_id FROM passages WHERE id=?", (note["passage_id"],)).fetchone()["work_id"]
            if not self.can_review_work(work_id, user_id):
                raise DomainError("无权审阅该作品的校记")
            if action == "reject" and not reason.strip():
                raise DomainError("驳回必须写明原因")
            now = datetime.now().isoformat()
            self.conn.execute(
                "UPDATE char_notes SET status=?,reviewer_id=?,review_reason=?,reviewed_at=? WHERE id=?",
                ("approved" if action == "approve" else "rejected", user_id, reason.strip(), now, note_id),
            )
            if action == "approve":
                self.conn.execute(
                    "INSERT INTO lectiones(passage_id,witness_id,position,char,note_id,updated_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(passage_id,witness_id,position) DO UPDATE SET char=excluded.char,note_id=excluded.note_id,updated_at=excluded.updated_at",
                    (note["passage_id"], note["witness_id"], note["position"], note["proposed_char"], note_id, now),
                )

    def _stale_variant_notes(self, variant_id: int) -> None:
        rows = self.conn.execute(
            "SELECT id,passage_id,witness_id,position FROM char_notes WHERE variant_id=? AND status IN ('pending','approved')",
            (variant_id,),
        ).fetchall()
        if not rows:
            return
        self.conn.execute(
            "UPDATE char_notes SET status='stale' WHERE variant_id=? AND status IN ('pending','approved')", (variant_id,)
        )
        for row in rows:
            removed = self.conn.execute(
                "DELETE FROM lectiones WHERE passage_id=? AND witness_id=? AND position=? AND note_id=?",
                (row["passage_id"], row["witness_id"], row["position"], row["id"]),
            ).rowcount
            if removed:
                fallback = self.conn.execute(
                    "SELECT id,proposed_char FROM char_notes WHERE passage_id=? AND witness_id=? AND position=? AND status='approved' "
                    "ORDER BY id DESC LIMIT 1",
                    (row["passage_id"], row["witness_id"], row["position"]),
                ).fetchone()
                if fallback:
                    self.conn.execute(
                        "INSERT INTO lectiones(passage_id,witness_id,position,char,note_id,updated_at) VALUES(?,?,?,?,?,?)",
                        (row["passage_id"], row["witness_id"], row["position"], fallback["proposed_char"], fallback["id"], datetime.now().isoformat()),
                    )

    def list_char_notes(self, passage_id: int, user_id: int) -> list:
        passage = self.conn.execute("SELECT work_id FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage or not self.can_view_work(passage["work_id"], user_id):
            raise DomainError("无权查看该段落校记")
        return [dict(r) for r in self.conn.execute(
            "SELECT c.*,a.name AS author_name,r.name AS reviewer_name FROM char_notes c "
            "JOIN users a ON a.id=c.author_id LEFT JOIN users r ON r.id=c.reviewer_id "
            "WHERE c.passage_id=? ORDER BY c.position,c.id", (passage_id,),
        )]

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
        pending = 0
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
                tokens = tokenize_aligned(item["aligned_text"])
                applied = []
                for lec in self.conn.execute(
                    "SELECT l.position,l.char,l.note_id,c.original_char,u.name AS author FROM lectiones l "
                    "JOIN char_notes c ON c.id=l.note_id JOIN users u ON u.id=c.author_id "
                    "WHERE l.passage_id=? AND l.witness_id=? ORDER BY l.position",
                    (passage["id"], item["witness_id"]),
                ).fetchall():
                    if 1 <= lec["position"] <= len(tokens):
                        tokens[lec["position"] - 1] = lec["char"]
                        applied.append({"position": lec["position"], "original_char": lec["original_char"],
                                        "char": lec["char"], "note_id": lec["note_id"], "author": lec["author"]})
                item["reading_text"] = "".join(tokens)
                item["approved_positions"] = applied
                item["pending_positions"] = [dict(r) for r in self.conn.execute(
                    "SELECT c.id,c.position,c.original_char,c.proposed_char,c.evidence,c.variant_id,c.layer,u.name AS author "
                    "FROM char_notes c JOIN users u ON u.id=c.author_id "
                    "WHERE c.passage_id=? AND c.witness_id=? AND c.status='pending' ORDER BY c.position",
                    (passage["id"], item["witness_id"]),
                )]
                pending += len(item["pending_positions"])
                alignments.append(item)
            variants = []
            for row in self.conn.execute("SELECT * FROM variants WHERE passage_id=? ORDER BY witness_id,layer,id", (passage["id"],)).fetchall():
                variant = dict(row)
                variant["notes"] = [dict(r) for r in self.conn.execute("SELECT * FROM notes WHERE variant_id=? ORDER BY id", (row["id"],))]
                variants.append(variant)
            passages.append({**dict(passage), "alignments": alignments, "variants": variants})
        return {"work": dict(work), "witnesses": witnesses, "passages": passages, "gap_count": gaps, "pending_note_count": pending}

    def snapshot(self) -> dict:
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "works": [dict(r) for r in self.conn.execute("SELECT * FROM works ORDER BY id")],
            "witnesses": [dict(r) for r in self.conn.execute("SELECT * FROM witnesses ORDER BY id")],
            "passages": [dict(r) for r in self.conn.execute("SELECT * FROM passages ORDER BY id")],
            "char_notes": [dict(r) for r in self.conn.execute(
                "SELECT id,passage_id,witness_id,variant_id,layer,position,original_char,proposed_char,status,author_id,reviewer_id "
                "FROM char_notes ORDER BY id")],
        }
