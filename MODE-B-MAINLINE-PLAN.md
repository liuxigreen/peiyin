# Mode B 主流程收敛计划

目标：上传混音音频 + 中文字幕/英文字幕，输出可直接导入剪映的完整配音 WAV、SRT/ASS 和逐句音频包。

## 唯一生产链

`upload → probe → separate-vocals → diarize → bind-speakers → align/translate → tts-batch → fit/qc → master/package`

`mode-b/run` 只能负责创建/推进这条链，禁止直接调用 mock TTS。

## 初期 3060 模型策略

- 分离：Demucs `htdemucs`；显存不足时切 `mdx_extra_q`。
- 声纹：SpeechBrain ECAPA 聚类；暂不依赖 pyannote/HF gated 模型。
- 克隆：CosyVoice3 0.5B 作为默认 provider，参考音必须来自分离后的 vocals。
- 备用：OpenVoice/Fish Speech 通过 provider 接口接入，不在主流程中硬编码。

## 产物契约

- 每阶段写入 `PipelineRun` 状态、输入指纹、产物路径和错误信息。
- 真实 TTS 产物缺失时失败，不能生成正弦波或其它占位音频。
- 最终包固定包含 `master_13LUFS.wav`、目标语言 `.srt`/`.ass`、`audio/*.wav`、`manifest.json`、`qc_report.json`。

## 实施顺序

1. 将 provider、分离产物和 TTS 任务状态接入同一 run 状态机。
2. `mode-b/run` 自动创建分离任务，分离完成后再创建声纹/角色绑定任务。
3. 翻译完成后自动调用真实 `tts-batch`，禁止 `tts_clips_mock`。
4. TTS 全部完成后自动构建交付包，网页 DAG 显示真实阶段。
5. 用白月光做小批量纵向验收，再扩到全量。
