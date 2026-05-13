# Task 2 (pymatchmaker 統合) 作業 TODO — Issue #3

## 環境セットアップ (ユーザ作業)
- [ ] WSL2 + Ubuntu 24.04 をインストール (`wsl --install -d Ubuntu-24.04`)
- [ ] Ubuntu 初期ユーザ設定
- [ ] 必須パッケージ: `python3-pip python3-venv python3-tk fluidsynth libfluidsynth-dev portaudio19-dev`
- [ ] Chromium 動作確認 (WSLg のオーディオ/GUI パススルー)

## コード変更 (Claude 作業)
- [x] Issue #3 起票
- [x] `feat/3-pymatchmaker-integration` ブランチ作成
- [ ] `core/matcher.py` を `Matchmaker.run()` ラッパに書き換え
- [ ] `core/slide_controller.py` (Playwright + Queue) 新規作成
- [ ] `core/audio_capturer.py` 削除
- [ ] `core/feature_extractor.py` 削除
- [ ] `core/__init__.py` のエクスポート整理
- [ ] `main.py` を新アーキテクチャに合わせて簡素化
  - audio/feature ワーカー起動を削除
  - SlideController 起動を追加
  - `_matching_loop` を最新値ポーリングに変更
  - `_execute_action` を Playwright 呼び出しに変更
  - CLI 引数 `--slide-url` 追加
- [ ] `requirements.txt` 更新 (pymatchmaker, playwright, sounddevice)

## 設定・ドキュメント
- [ ] `config_example.json` の action 値説明をコメント or README に追記
- [ ] `INSTALLATION.md` / `INSTALLATION_JP.md` を WSL2 手順に書き換え
- [ ] `README.md` / `README_JP.md` を新使用フローに更新
- [ ] `COMPLETION.md` のモック表記を解除
- [ ] `sequential_live_follower/README.md` を新 API で更新

## 検証 (WSL2 セットアップ完了後)
- [ ] `python -c "from matchmaker import Matchmaker; print('ok')"` 成功
- [ ] サンプル MusicXML で `Matchmaker.run()` が拍位置を返す smoke test
- [ ] サンプル Google Slides 共有 URL で Playwright が起動・キー送信成功
- [ ] エンドツーエンド: `python -m sequential_live_follower.main config.json --slide-url <URL> -v` で動作確認
- [ ] N キーで次の楽章ロード切替

## 完了処理
- [ ] Issue #3 へ完了コメント (変更ファイル一覧と動作確認結果)
- [ ] PR 作成 `(Closes #3)`
- [ ] レビュー → master マージはユーザ許可後

## レビューセクション (作業完了後に記入)
_この欄は実装完了後に埋める_
