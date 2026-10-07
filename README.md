# RTCM 実行予算・落付見込 AI取込

監視フォルダへ配置されたPDFまたはXDWを社内生成AIのWeb画面へ送り、次のJSONを検証したうえでOracleの `T_JYOSAN` に新版を登録します。RTCM登録が正常にコミットされた後、同じ需要家情報をC1の倉庫登録へ自動登録します。

```json
{"seiban":"L1801","pdf_hansu":0,"otkmkmnoki":"20250210","otkmkmcost":52000,"demand_customer_code":"109155004","demand_customer_name":"太陽誘電","demand_customer_name_kana":"ﾀｲﾖｳﾕｳﾃﾞﾝ","demand_customer_postal_code":"3701196","demand_customer_address":"佐波郡玉村町川井1796-1","demand_customer_phone":"0270-65-7121"}
```

## 全体の処理

1. `RTCM_WATCH_FOLDER` の直下だけを監視します。PDF/XDWを検出した時点でSHA-256、元パス、格納日時、作業用コピーのパスを `processing_state.sqlite3` に `queued` として登録し、`logs\watcher.jsonl` にもキュー登録ログを出力します。サブフォルダは監視しません。
2. 月曜～金曜の `RTCM_BATCH_TIME`（既定 `08:15`）になったら、その時刻までに格納されたファイルをまとめて処理します。8:15より後の追加分と土日追加分は、次の平日8:15まで待機します。
3. PDFは作業フォルダへコピーします。XDWはDocuWorks Viewerから「DocuWorks PDF」プリンタへ印刷します。プリンタが `DOCUWORKS_PDF_OUTPUT_FOLDER` へ自動出力したPDFを検出し、作業フォルダへコピーします。
4. Edgeで生成AIの指定ユースケースを開き、PDFを「ファイル添付」へ設定して「実行」を押します。
5. 結果欄のJSONを取得・検証します。
6. PDF版数が初版（0版）であることを確認します。1版以上はRTCMへ登録せずスキップします。
7. 初版だけを `T_JYOSAN` へ登録します。
8. `C1_WAREHOUSE_ENABLED=true` の場合、RTCMコミット成功後にC1へログインし、需要家倉庫を登録します。RTCMがスキップまたは失敗した場合はC1登録を行いません。
9. `RTCM_ARCHIVE_PDF_ENABLED=true` の場合だけ、DB更新成功後に処理対象PDFを監視フォルダ内の実行年 `YYYY` フォルダへ移動します。既定は `false` です。
10. 成功／失敗とRTCM・C1の更新内容をメール通知し、ファイルのSHA-256と結果をSQLiteへ記録します。同じ内容のファイルは再登録しません。

生成AIが抽出した製番が `U` で始まる場合は、RTCMへ登録せずに処理をスキップします。スキップ結果はログへ記録し、メール通知が有効な場合は「スキップ」として通知します。

生成AIが抽出したPDF版数が1以上の場合も、Oracleへ接続せずRTCM登録をスキップします。スキップ理由とPDF版数はログ、結果JSON、メールへ出力します。

PDF選択直後には実行せず、生成AI画面の添付ファイル一覧にPDF名が表示されるまで待機します。表示後、実行ボタンが有効であることを確認してからクリックします。待機上限は `GENAI_UPLOAD_TIMEOUT_SECONDS` で設定できます。

生成AIへアップロードする際は、元ファイルを変更せず、作業フォルダへコピーしたPDFのファイル名から半角・全角を含む空白文字を除去します。例えば `V3579 R1 DIC.pdf` は `V3579R1DIC.pdf` としてアップロードします。XDWから変換したPDFにも同じ規則を適用します。XDWの印刷用コピーには内容ハッシュを付けるため、固定出力フォルダで同名警告が発生しません。

PDF選択後は、添付欄に対象ファイル名が表示され、アップロード中のスピナーが消えた状態が1秒間継続するまで待機してから「実行」を押します。待機上限は `GENAI_UPLOAD_TIMEOUT_SECONDS`（既定180秒）です。

生成AIの回答は途中経過を逐次表示するため、結果欄に文字が現れただけでは完了と判定しません。RTCMの4項目と需要家の6項目を含む完全なJSONとして検証できるまで待機します。

生成AI URL:

```text
https://genu.n-coke.com/use-case-builder/execute/7ef1d4ea-837d-4870-baf4-28e7cd35bba9
```

ブラウザ監視では同梱の `prompt.txt` を画面へ自動送信しません。生成AIのユースケース設定も `prompt.txt` と同じ10項目を出力する内容へ更新してください。旧形式のままでは、需要家項目不足として処理エラーになります。

## 登録仕様

- PDF記載の改訂番号を `pdf_hansu` として取得し、`pdf_hansu = 0` の場合だけ登録します。
- `T_JUCHUZAN_CTL` から現在の受注金額 `JUCHUKG` を取得します。
- 既存行がある場合、最大 `HANSU` の全項目を引き継ぎ、`HANSU + 1` でINSERTします。
- 既存行がない場合は `HANSU = 0` でINSERTし、未指定列はテーブルの既定値を使用します。
- `RECNO` は `SQ_T_JYOSAN01.NEXTVAL` で採番します。
- `OTKMKMNOKI` と `OTKMKMCOST` はAI抽出値で上書きします。
- `JKYSNHANKG` と `OTKMKMHNKG` は現在の `T_JUCHUZAN_CTL.JUCHUKG` を設定します。
- 実行予算・落付見込の益金と益率を再計算します。益率は小数第2位まで0方向に切り捨て、±999.99に制限します。
- 書込み先は `T_JYOSAN` だけです。`T_JUCHUZAN_CTL` は参照と同時実行制御のためにロックしますが更新しません。
- `RTCM_PGID` には、画面用の `AK01200G` ではなくAI取込専用のPGIDを推奨します。

### C1倉庫登録仕様

- C1登録はRTCMのコミット成功後だけ実行します。`--dry-run` ではC1へ書き込まず登録予定値だけを結果に出します。
- 汎用資格情報名は `C1_Auto_Fill: 192.168.9.98` です。未登録時はブラウザで手動ログインし、入力した資格情報をログイン成功後に自動保存します。
- メインメニューの「倉庫登録」（`AX040010`）を開きます。
- 倉庫コードは `C1_WAREHOUSE_CODE_PREFIX` と `C1_WAREHOUSE_CODE_START` から作り、C1照会で既存コードとの衝突を確認して最初の空き番号を採用します。同じ製番・需要家の登録を見つけた場合は再登録しません。
- 倉庫名称は需要家名称、表示名称は「製番 半角空白 需要家名称」、名称カナは「製番 半角空白 需要家名称カナ」とします。
- 郵便番号、住所、電話番号、所在地組織を設定し、その他の区分は添付の登録済倉庫と同じ画面既定値を使用します。
- C1登録後に倉庫コードを照会し、表示名称まで一致した場合だけ成功とします。
- C1処理で失敗した場合も、`rtcm_result.json` のコミット済みチェックポイントを使うため、再実行時に同じ `T_JYOSAN` を重複登録しません。

この実装は、例示されたHANSU=2から次の値を作ります。

| 項目 | 登録値 |
|---|---:|
| HANSU | 3 |
| JKYSNHANKG / OTKMKMHNKG | 4,800,000 |
| JKYSNCOST | 3,016,137（前版引継ぎ） |
| JKYSNEKIKG / JKYSNEKIRT | 1,783,863 / 37.16 |
| OTKMKMNOKI | 20261023 |
| OTKMKMCOST | 3,216,000 |
| OTKMKMEKI / OTKMKMEKIRT | 1,584,000 / 33.00 |

## セットアップ

Python 3.11以上、Microsoft Edge、DocuWorks Viewer、「DocuWorks PDF」プリンタを使用します。

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`config.example.env` を `config.env` へコピーして、接続情報とフォルダを設定してください。`config.env` は外部ライブラリに依存せず、プログラム起動時に自動で読み込まれます。監視プログラムはインストール済みのEdgeを使用するため、Playwright用ブラウザの追加ダウンロードは不要です。

```powershell
Copy-Item .\config.example.env .\config.env
notepad .\config.env
```

### メール通知

`notify.example.json` を `C:\RTCM_AI\notify.json` へコピーし、SMTPサーバーと通知先を設定します。`enabled` が `true` の通知先だけに送信します。設定ファイルが存在しない、または有効な通知先がない場合は通知しません。

```powershell
Copy-Item .\notify.example.json C:\RTCM_AI\notify.json
notepad C:\RTCM_AI\notify.json
```

メールは本登録時だけ送信し、`--dry-run` では送信しません。メール送信に失敗しても、コミット済みのRTCM更新を失敗扱いにはせず、`watcher.jsonl` に `notification_error` を記録します。

### ログイン情報

Windows資格情報マネージャーの「Windows資格情報」→「汎用資格情報」に、次の対象名で保存します。

```text
genu.n-coke.com
C1_Auto_Fill: 192.168.9.98
```

- ユーザー名は資格情報のユーザー名欄を使用します。
- パスワードは資格情報のパスワード欄から取得します。
- 資格情報がなくても、Edgeで手動ログインできます。
- 生成AIは手動ログイン後にコンソールで保存を選択します。C1は手動ログイン中の入力を保持し、ログイン成功後にC1用の汎用資格情報へ自動保存します。
- パスワードはログや結果ファイルへ出力しません。

## 監視実行

最初は必ず `--once --dry-run` で、現在置かれているファイル1件を処理し、DB登録がROLLBACKされることを確認してください。

```powershell
python .\watch_folder.py --once --dry-run
```

`--once` 指定時はスケジュールを待たず、現在のキューを手動で即時処理します。また、前回エラーになった同一ファイルも自動的に再試行します。

確認後、次のコマンドで継続監視します。

```powershell
python .\watch_folder.py
```

### EXE版

`build_exe.ps1` を実行すると、`dist\rtcm_ai_jyosan_import\` に次を作成します。

- `rtcm_ai_jyosan_import.exe`
- `config.env`
- `notify.json`
- `README.md`

EXEは同じフォルダにある外部設定ファイルを読み込みます。設定変更時にEXEを再作成する必要はありません。

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
.\dist\rtcm_ai_jyosan_import\rtcm_ai_jyosan_import.exe --once --dry-run
```

前回エラーになった同一ファイルを再試行するときは次のようにします。

```powershell
python .\watch_folder.py --once --retry-errors
```

通常のオプションなし起動では、処理に失敗したファイルを自動再実行しません。再実行する場合は `--retry-errors` または `--once` を指定してください。設定で自動再試行を有効にしたい場合のみ、`config.env` の `RTCM_AUTO_RETRY_ERRORS=true` とし、`RTCM_ERROR_RETRY_SECONDS` で再試行間隔（秒）を指定できます。

AI画面を使わずDB登録ロジックだけを確認する単発実行も残しています。

```powershell
python .\import_jyosan.py --json-file .\sample_v3579.json --expected-seiban V3579 --dry-run
```

## 作業フォルダとログ

`RTCM_WORK_FOLDER` の中には次が作成されます。

| パス | 内容 |
|---|---|
| `queue\<SHA256先頭16桁>\元ファイル名` | 格納時に作成する処理待ち用コピー。元ファイルが手動移動されても、このコピーから8:15に処理します。 |
| `<SHA256先頭16桁>\*.pdf` | XDWから変換したPDF |
| `<SHA256先頭16桁>\extracted.json` | 生成AIの抽出結果 |
| `<SHA256先頭16桁>\rtcm_result.json` | RTCMコミット済みチェックポイント。C1失敗後の再実行でRTCM重複登録を防止します。 |
| `<SHA256先頭16桁>\result.json` | RTCM・C1処理結果 |
| `processing_state.sqlite3` | `queued`、`processing`、`success`、`skipped`、`error`とキュー情報を保持する状態DB |
| `logs\watcher.jsonl` | キュー登録・成功・スキップ・エラー履歴 |

`RTCM_ARCHIVE_PDF_ENABLED=true` の場合、本登録に成功したファイルは `<RTCM_WATCH_FOLDER>\YYYY\` へ移動します。入力がXDWの場合は変換後PDFと元のXDWを移動し、入力がPDFの場合は元PDFを移動します。`false` の場合および `--dry-run` では移動しません。年別フォルダは監視対象外です。

DBコミット後のPDF移動に失敗した場合は、重複登録防止のためDB更新を成功扱いのまま保持し、結果・ログ・メールに保管エラーを記録します。

## 注意事項

- 本番投入前に、`T_JYOSAN` の主キーが `(KAICD, KYOTENCD, KOJCD, SEIBAN, HANSU)` であること、`T_JUCHUZAN_CTL.JUCHUKG` が現在販価として正しいことをテスト環境で確認してください。
- 初版では前版から引き継げない `OTMKMSDBMN`、販売分析項目、機種項目などはテーブル既定値になります。初版にも別テーブル由来の値が必要なら、その取得元を確定して追加してください。
- DocuWorks PDFのプリンタ名と固定出力先は、それぞれ `DOCUWORKS_PDF_PRINTER`、`DOCUWORKS_PDF_OUTPUT_FOLDER` で設定します。現在の想定出力先は `C:\DocuWorksPdfOut` です。
- 監視プログラムはEdgeとDocuWorksの画面を操作するため、ユーザーがログオンしている対話セッションで実行してください。Windowsサービスのセッション0では動作しません。
- 生成AI画面のHTML構造が変更された場合は、`genu_browser.py` のセレクター調整が必要です。
- C1画面のHTML構造や倉庫コード採番ルールが変更された場合は、`c1_browser.py` とC1設定値の調整が必要です。
