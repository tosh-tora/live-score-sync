# Task 2 (pymatchmaker 統合) 作業 TODO — Issue #3

## 環境セットアップ (ユーザ作業)
- [ ] WSL2 + Ubuntu 24.04 をインストール (`wsl --install -d Ubuntu-24.04`)
- [ ] Ubuntu 初期ユーザ設定
- [ ] 必須パッケージ: `python3-pip python3-venv python3-tk fluidsynth libfluidsynth-dev portaudio19-dev`
- [ ] Chromium 動作確認 (WSLg のオーディオ/GUI パススルー)

## コード変更 (Claude 作業)
- [x] Issue #3 起票
- [x] `feat/3-pymatchmaker-integration` ブランチ作成
- [x] `core/matcher.py` を `Matchmaker.run()` ラッパに書き換え
- [x] `core/slide_controller.py` (Playwright + Queue) 新規作成
- [x] `core/audio_capturer.py` 削除
- [x] `core/feature_extractor.py` 削除
- [x] `core/__init__.py` のエクスポート整理 (元々空のためそのまま)
- [x] `main.py` を新アーキテクチャに合わせて簡素化
  - [x] audio/feature ワーカー起動を削除
  - [x] SlideController 起動を追加
  - [x] `_matching_loop` を最新値ポーリングに変更 (`_state_sync_loop` に改称)
  - [x] `_execute_action` を Playwright 呼び出しに変更
  - [x] CLI 引数 `--slide-url` 追加
  - [x] N キーを Tk バインドに変更 (`keyboard` 依存削除)
- [x] `requirements.txt` 更新 (pymatchmaker, playwright, sounddevice)
- [x] commit `e8e97ba` で Issue #3 にリンク

## 設定・ドキュメント
- [x] `INSTALLATION_JP.md` を WSL2 手順に書き換え
- [x] `README_JP.md` を新使用フローに更新
- [ ] `INSTALLATION.md` (英語版) を WSL2 手順に書き換え (PR 前に着手)
- [ ] `README.md` (英語版) を新使用フローに更新 (PR 前に着手)
- [ ] `COMPLETION.md` / `COMPLETION_JP.md` のモック表記を解除 (PR 前に着手)
- [ ] `sequential_live_follower/README.md` / `README_JP.md` を新 API で更新 (PR 前に着手)
- [ ] `config_example.json` の action 値説明 (README で十分カバー済み、要否判定)

## 検証 (WSL2 セットアップ完了後)
- [x] `python -c "from matchmaker import Matchmaker; print('ok')"` 成功
- [x] サンプル MusicXML で `Matchmaker.run()` が拍位置を返す smoke test
- [x] サンプル Google Slides 共有 URL で Playwright が起動・キー送信成功
- [x] エンドツーエンド: `python -m sequential_live_follower.main config.json --slide-url <URL> -v` で動作確認
- [x] N キーで次の楽章ロード切替

## 完了処理
- [x] Issue #3 へ完了コメント (変更ファイル一覧と動作確認結果)
- [x] PR 作成 `(Closes #3)` → PR #4
- [ ] レビュー → master マージはユーザ許可後

## レビューセクション (作業完了後に記入)

### 動作確認結果 (2026-05-14)

**環境**: WSL2 Ubuntu 24.04 + WSLg / Python 3.12 / sounddevice 経由 Realtek マイク

| 確認項目 | 結果 |
|---------|------|
| `from matchmaker import Matchmaker` | ✅ OK |
| `import partitura` | ✅ OK |
| `from playwright.sync_api import sync_playwright` | ✅ OK |
| sounddevice デバイス検出 | ✅ OK (ALSA→PulseAudio ブリッジ経由) |
| Chromium 起動 + Google Slides 表示 | ✅ OK |
| Tkinter 操作 GUI 表示 | ✅ OK |
| N キー → 次の楽章ロード | ✅ OK (`'N' key pressed → loading next movement`) |
| エンドツーエンド起動 | ✅ OK |

**WSL2 オーディオ追加セットアップ**: `libasound2-plugins` + `~/.asoundrc` (ALSA default → pulse) が必要だった。`INSTALLATION_JP.md` に追記要。

---

## Issue #28 / #29 後の残課題: 2x 先走り問題の診断 (2026-05-17)

修正済の慣性外挿撤去 (#28) と拍子記号 ndarray 解釈 (#29) を WSL2 で適用後、運命冒頭 (2/4) の GUI カウントが演奏より約 2 倍速で進む報告。pymatchmaker のソース確認では理論上 2x になる単位ミスマッチはなく、観測データなしには根本原因が特定できない。

### 環境変数フラグ

| 変数 | 効果 |
|---|---|
| `SLF_BEAT_LOG=path.csv` | pymatchmaker が emit する毎ビートを CSV (wall_iso, monotonic_s, raw_beat, score_file) で記録 |
| `SLF_VERBOSE_SYNC=1` | `-v` 時の state-sync DEBUG ログのレート制限 (1Hz) を外し、20Hz で出力 |

### 検証プロトコル A: メトロノーム較正 (必須)

1. iPhone 等で 60 BPM のメトロノーム音を 30 秒間マイクに入力する
   ```bash
   SLF_BEAT_LOG=metro_60bpm.csv python -m sequential_live_follower.main config.json --slide-url <URL> -v
   ```
2. 終了後、CSV から `(raw_beat[t≈30s] - raw_beat[t≈10s]) / 20` を計算 (= 平均 beats/sec)
3. 期待値:
   - **60 BPM (4分音符) ≒ 1.0 beats/sec** が期待値 (pymatchmaker は quarter-note 単位)
   - 観測 1.0 ± 0.1 → pymatchmaker は正しく単位を返している → DTW アラインメント側の問題
   - 観測 2.0 ± 0.2 → pymatchmaker が 2x の単位で返している → 我々の wrapper / score_mapper 側で /2 する必要
   - 観測がばらつく / 線形でない → DTW 自体の安定性問題
4. CSV のパスと主要数値を共有してもらい、次の手 (DTW パラメータ調整 or 単位補正) を決める

### 検証プロトコル B: 既知録音再生 (任意)

別マシンで運命冒頭の演奏録音 (BPM が分かるもの) をスピーカー再生 → マイク収録。CSV をプロットして raw_beat 時系列が線形 (= 安定追従) か、階段状ジャンプ (= DTW misalignment) かを目視判別。

### 観測後の追加修正 (要観測値)

| 観測パターン | 想定原因 | 対策候補 |
|---|---|---|
| 2x ぴったり | pymatchmaker 単位の誤解釈 | `Matchmaker(..., tempo=N)` 明示、または score_mapper 側で /2 |
| 概ね線形だが時々ジャンプ | DTW misalignment | `method="dixon"`、`feature_type="mel"`/`"logspectral"` 試行 |
| 線形 1x | 単に古いブランチ / 設定 | ユーザー再確認 |

