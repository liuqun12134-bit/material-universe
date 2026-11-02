# 库记录与索引

记录是 UTF-8 JSON 文字文件，可由不同 agent 直接读写；复杂观察可附 Markdown 和图片。模板在 [demo-record.json](../templates/demo-record.json) 与 [experience-record.json](../templates/experience-record.json)。模板是结构样例，不是已入库证据；复制后必须填入本次事实。

## 共用字段

`schema_version=1`；id 为不重复的小写字母/数字/连字符，必须等于目录名；title；record_type 为 demo 或 experience；modes 为 talk/use 的非空列表（两者通用就都填）；category 非空；product_name 可 null；product_form/container/body_part 可 null；tags 列表；created_on/updated_on 为 YYYY-MM-DD；summary 非空。品类可以逐步扩展，不维护固定面膜分支。

结构：`demo-library/entries/ID/record.json` 或 `experience-library/entries/ID/record.json`。所有资产引用相对于这份 record.json 所在目录；路径不得绝对、不得含 .. 越界、不得是网址；路径对应的文件必须真实存在并位于该条目内部。附件可存图片、提示词、来源观察；整段视频是否随包携带如实登记。

## 演示字段

provenance：kind 是 video_analysis（本次实际分析视频）、legacy_text（迁移历史文字）或 authored_method（设计方法，未经视频验证）；description、observation_method、limitations；source_url 可 null 或公开 http(s)网址；source_asset 为包内视频相对路径或 null；notes_path 为来源观察文件相对路径或 null；analyzed_on 为实际分析日（未知可 null）；duration_seconds 为实测值或 null；time_reliability 为 measured/approximate/unknown/not_applicable。

preconditions 列表描述适用的起始条件。units 非空，每个单元含 id、name、source_time（区间说明或 null）、start_conditions、action、transition、end_state、expected_effect、avoid、prompt_fragment。单元 id 在条目内唯一。动作和效果以此关联；不只存“自然涂抹、效果好”。图像 keyframes 为列表，每项 path、timestamp_seconds（未知 null）、role 和 observation。没有帧就空列表并在来源限制说明。

## 经验字段

status 仅 unconfirmed 或 user_confirmed_effective；context 包含 first_frame_state、problem、model、model_version（未知 null）、duration_seconds（未知 null）及 settings；applicability/unknowns 为列表。attempts 非空，每次含唯一 id、date、observations、hypotheses、changes、prompt_path（相对路径或 null）、outcome、feedback。

outcome 是 unverified/failed/improved/succeeded；这是尝试结果，不是第三套确认状态。feedback 为 null，或包含 author=user、date、text、resolved、remaining 的对象。自动评分不能伪装成 user 反馈。

confirmed_attempt 在未确认时为 null；已确认时必须引用同案例中含明确用户有效反馈、outcome=improved/succeeded 的尝试，confirmed_scope 非空。历史成功尝试可保留在状态已退回的案例中。每轮新修订追加尝试，原先失败和反馈不覆盖。

attachments 为相对路径列表，可保存首帧、成片、原音频、来源说明；不要求每次复制所有大文件。related_demo_ids 列表可空；非空时引用必须存在，避免经验指向丢失演示。

## 索引与更新

两个 index.json 从记录派生，包含 ID、标题、模式、品类、形态、包装、部位、产品名、标签、摘要、来源类型/状态与相对路径。先读索引再读取匹配记录；索引不是另一套事实来源。

写入条目后 `python scripts/library.py reindex` 会先检查记录，再原子替换索引；validate 检查记录、资产、相对路径、引用关系和索引是否过期。它不会判断视频质量、反馈真假或模型能力。

维护同一份可写目录；并发修改同一条目前先读最新文件，合并本轮变更，不回写过期副本。同步或升级保留用户记录；同 ID 不同内容需要合并，不静默选远程版本。用户授权保存本地不自动等于授权公开传播原素材。
