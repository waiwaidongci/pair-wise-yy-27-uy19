# 数字人文文本校勘

这是一个 Python 标准库实现的校勘工作台，使用 SQLite 保存作品、版本、残片、转录、段落、异文、注释、修订层和快照，并通过 `http.server` 暴露 JSON API。

## 启动与测试

```bash
python app.py
python -m unittest discover -s tests -v
```

默认端口 `8114`，地址 <http://127.0.0.1:8114>。首次启动创建一个带缺页残片和不可辨标记的示例。数据库可通过 `COLLATION_DB` 指定，端口可通过 `PORT` 指定。

## 业务规则

- 版本类型限定为 `version`、`fragment`、`transcription`。
- 段落和版本必须属于同一作品，同一版本不能重复对齐同一段落。
- 只有负责人或被单独授权的编辑可以修改对应版本；其他用户只有查看权限。
- `[缺页]`、`[不可辨]`、`[残损]` 等标记会参与校勘稿导出和缺口统计，不匹配的方括号会拒绝保存。
- 每次新增或修改异文都会产生递增修订号和 JSON 快照；提交必须携带 `expected_revision`，旧页面不能覆盖新层。
- 锁定段落由负责人执行，锁定后任何新修订都会被拒绝。
- 字位校记按对齐文本序号登记（序号从 1 起，普通字符一位、`[不可辨]` 等标记整体一位），登记时系统自动记录当前原字；拟字限单个字位，依据至少 3 字。
- 一个字位只保留一张待审/已通过校记；`[缺页]`、`[残损]` 缺口不能当作确定文字释读，越界序号同样拒绝。
- 校记只能由具审阅权且非登记人本人的用户审定；通过后拟字叠加为该对齐的**当前释文**，驳回必须写明原因退回登记人。
- 审定时再次核对字位：对齐层字位已移动或原字变化的一律拒绝，需重新登记。
- 异文层发生变化（新增/修订异文、修订号推进）后，待审与已通过校记自动置为失效（`stale`），需由编辑重新核对字位后提交、重新走审阅；旧页面带过期 `expected_revision` 的审定按版本冲突拒绝。

## 主要接口

- `POST /api/users`、`POST /api/works`
- `POST /api/works/{id}/witnesses`、`POST /api/witnesses/{id}/editors`
- `POST /api/works/{id}/passages`、`POST /api/works/{id}/access`
- `POST /api/alignments`
- `POST /api/variants`、`POST /api/variants/{id}/revisions`
- `GET /api/passages/{id}/snapshots/{revision}?user_id=...`
- `POST /api/passages/{id}/lock`
- `GET /api/works/{id}/collation?user_id=...`
- 字位校记：`POST /api/emendations`（登记）、`GET /api/emendations?passage_id=&witness_id=&user_id=`（列表）、`POST /api/emendations/approve`、`POST /api/emendations/reject`（body 含 `emendation_id`、`reviewer_id`、驳回 `reason`，可选 `expected_revision`）、`POST /api/emendations/{id}/resubmit`（失效/驳回后按当前层重核提交）

导出接口把版本对齐、异文、注释、残损缺口和锁定状态组合成可复核的校勘稿；每个对齐另给 `reading`（叠加已通过校记的当前释文）与 `pending_positions`（待审/失效/驳回的未决字位），顶层给出 `unresolved_count` 未决字位数。页面底部的「校勘稿导出」直接展示释文与未决字位。
