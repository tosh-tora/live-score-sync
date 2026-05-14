# インストール & 検証ガイド

このシステムは 2 つのタスクで構成されます:

- **Task 1: MusicXML Compressor** — フルスコアからガイド譜を生成 (CLI ツール)。Windows / WSL2 / Linux / macOS どこでも動きます。
- **Task 2: Sequential Live Follower** — マイク追従によるスライド制御 (リアルタイムアプリ)。**WSL2 (Ubuntu) 上での動作を前提**としています。

> `pymatchmaker` (DTW 追従ライブラリ) は Windows wheel が公開されておらず、conda 経由のソースビルドも環境差で失敗が多いため、Task 2 は WSL2 (Windows Subsystem for Linux 2) で動かす方針です。Windows 11 の WSLg がマイク入力・GUI 描画をホストとシームレスに橋渡しするので、操作 GUI もスライド表示もすべて WSL2 内で完結します。

---

## Task 1 (Compressor) のインストール

Task 1 は単独で動く軽量スクリプトです。Windows でも WSL2 でも動きます。

```bash
# 任意の Python 3.10+ 環境で
pip install -r requirements.txt
```

使用例:

```bash
# work/inbox/ にある全 MusicXML をバッチ処理
python compressor.py

# 単一ファイル
python compressor.py work/inbox/score.xml

# カスタム重み
python compressor.py -c compressor_config.json --weight-rhythm 5.0
```

---

## Task 2 (Sequential Live Follower) のインストール — WSL2 セットアップ

### 1. WSL2 + Ubuntu のインストール (Windows ホスト側)

**管理者で PowerShell を起動して実行:**

```powershell
wsl --install -d Ubuntu-24.04
```

再起動を求められたら従ってください。再起動後 Ubuntu が自動で立ち上がるので、初期ユーザ名 / パスワードを設定します。

### 2. システムパッケージのインストール (Ubuntu 内)

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y \
    python3 python3-pip python3-venv python3-tk \
    fluidsynth libfluidsynth-dev \
    portaudio19-dev \
    libnss3 libatk-bridge2.0-0 libcups2 libgtk-3-0 libgbm1 libasound2t64 \
    fonts-noto-cjk \
    git
```

> `python3-tk` は操作 GUI (Tkinter) の表示に必要、`libnss3` 以降は Playwright が起動する Chromium の依存、`fonts-noto-cjk` は操作 GUI で日本語ファイル名・ラベルを豆腐化させないために必要です。

### 3. プロジェクトの取得と Python 環境構築

```bash
# /mnt/c (= Windows ファイルシステム) より WSL2 ネイティブ側に置く方が高速
cd ~
git clone https://github.com/tosh-tora/live-score-sync.git
cd live-score-sync

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
```

### 4. ALSA → PulseAudio ブリッジの設定

WSL2 では PortAudio (sounddevice) が直接 ALSA ハードウェアにアクセスできません。PulseAudio 経由でルーティングするブリッジを設定します。

```bash
# ALSA の PulseAudio プラグインをインストール
sudo apt install -y libasound2-plugins pulseaudio

# ALSA のデフォルトデバイスを PulseAudio に向ける
cat > ~/.asoundrc << 'EOF'
pcm.default pulse
ctl.default pulse
pcm.pulse {
    type pulse
}
ctl.pulse {
    type pulse
}
EOF
```

設定後、sounddevice からデバイスが見えることを確認：

```bash
python -c "import sounddevice as sd; print(sd.query_devices())"
# → "pulse" と "default" が表示されれば OK
```

### 5. マイク確認

```bash
# sounddevice 経由でデバイス一覧を確認 (pymatchmaker が使うバックエンド)
python -c "import sounddevice as sd; print(sd.query_devices())"
```

> **注意**: `arecord` コマンドは WSL2 の ALSA ハードウェアが存在しないためエラーになりますが、sounddevice にデバイスが表示されれば問題ありません。

### 6. 動作確認 (smoke test)

```bash
# pymatchmaker / partitura / Playwright のインポート
python -c "from matchmaker import Matchmaker; print('matchmaker OK')"
python -c "import partitura; print('partitura OK')"
python -c "from playwright.sync_api import sync_playwright; print('playwright OK')"
```

すべて `OK` が出れば準備完了です。

---

## Task 2 の実行

```bash
# 仮想環境を有効化
source ~/live-score-sync/.venv/bin/activate
cd ~/live-score-sync

# config.json を作成 (初回のみ)
cp sequential_live_follower/config_example.json config.json
# config.json を編集して xml_file / triggers を実ファイルに合わせる

# 起動
python -m sequential_live_follower.main config.json \
    --slide-url "https://docs.google.com/presentation/d/<ID>/present" \
    -v
```

起動時の挙動:

1. Playwright が Chromium を非ヘッドレスで開き、指定の Google Slides URL に遷移
2. Tkinter の操作 GUI (現在小節・信頼度・次トリガー表示) が別ウィンドウで起動
3. 最初の楽章 (config.json の `movements[0]`) が自動ロードされ、マイク追従開始

**拡張ディスプレイ運用:**

- WSLg で起動した Chromium ウィンドウをマウスでプロジェクタ側のモニタにドラッグ
- F11 (または Google Slides の「スライドショーを開始」ボタン) でフルスクリーン
- 操作 GUI は手元モニタに残す
- 操作 GUI が**フォーカスを持っている時** `N` キーを押すと次の楽章に進みます

---

## Google Slides の URL について

Google Slides のプレゼン URL は以下の形式が便利です:

```
https://docs.google.com/presentation/d/<PRESENTATION_ID>/present?slide=id.p
```

- `/present` 形式にするとロード直後にプレゼンテーションモード (全画面相当) になる
- スライドは「リンクを知っている全員 (閲覧者)」の共有設定にしておく
- ローカルファイル (`.pptx`) を使いたい場合は事前に Google Slides にアップロードして変換

---

## トラブルシューティング

| 症状 | 原因 | 対処 |
|------|------|------|
| `from matchmaker import Matchmaker` で ImportError | WSL2 内で pip install されていない | `pip install pymatchmaker` を venv 有効化済みで実行 |
| `Could not find FluidSynth library` | libfluidsynth-dev 未導入 | `sudo apt install fluidsynth libfluidsynth-dev` |
| マイクが認識されない | WSLg オーディオドライバの初期化失敗 | `wsl --shutdown` → 再起動。Windows 側の入力デバイスがデフォルトに設定されているか確認 |
| Chromium 起動でクラッシュ | `playwright install chromium` 未実行 | venv 有効化済みで `playwright install chromium` |
| トリガー時にスライドが進まない | Chromium がフォーカスを失っている / プレゼンモードでない | Chromium ウィンドウをクリック → F11 でフルスクリーンに |
| 信頼度が常に低い | マイクゲイン低 / 楽器配置と離れている | 入力ゲインを上げる、`config.json` の `confidence_threshold` を下げる |

---

## 受け入れ基準 (動作確認チェックリスト)

- [ ] `pip install -r requirements.txt` + `playwright install chromium` が完了
- [ ] `arecord` で WSL2 内からマイク録音できる
- [ ] `python -c "from matchmaker import Matchmaker; m = Matchmaker(score_file='sample.xml', input_type='audio')"` がエラーなく完了 (要 MusicXML サンプル)
- [ ] `python -m sequential_live_follower.main config.json --slide-url <URL>` で Chromium と Tkinter GUI が両方表示される
- [ ] 操作 GUI で `N` を押すと次の楽章に切り替わる
- [ ] 指定小節到達時にスライドが進む
- [ ] 信頼度が低下したとき GUI に "⚠ INERTIA MODE" が表示される
