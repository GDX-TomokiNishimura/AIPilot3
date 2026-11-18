# AIPilot3

## Teamsの1対1チャットを取り込む

Python 3.11以上で実行する単発バッチです。既存のデータ基盤がないため、初期の保存先にはSQLiteを使用します。Pythonの標準ライブラリだけで実行できます。

### Microsoft Entra IDの設定と管理者承認

1. Microsoft Entra管理センターの **アプリの登録 → 新規登録** で、この組織ディレクトリのみに対応するアプリを作成します。ディレクトリ（テナント）IDとアプリケーション（クライアント）IDを控えます。
2. **APIのアクセス許可 → アクセス許可の追加 → Microsoft Graph → アプリケーションの許可** で `Chat.Read.All` を追加します。この権限でチャット種別の確認とメッセージの取得を行えます。権限の範囲はテナント全体です。バッチは設定されたチャットIDだけを使用します。
3. 特権ロール管理者など、Microsoft Graphのアプリケーション権限を承認できる管理者が、同じ画面で **組織に管理者の同意を与えます** を選び、権限を確認して承認します。一覧に承認済みと表示されることを確認します。
4. **証明書とシークレット** でクライアントシークレットを発行し、値をシークレットマネージャーに保存します。実行時にシークレットマネージャーから `TEAMS_CLIENT_SECRET` として注入します。値をソース、コマンドライン引数、ログに記録しないでください。

対象のチャットにTeamsアプリをインストールしてリソース固有の同意（RSC）を利用できる環境では、同じバッチをチャット単位に制限した権限でも実行できます。その構成ではTeamsアプリのマニフェストで `ChatSettings.Read.Chat` と `ChatMessage.Read.Chat` をアプリケーション権限として宣言します。管理者がTeamsのアプリおよびRSCポリシーを確認し、許可されたユーザーが対象チャットへのインストール時に同意します。RSC構成では `Chat.Read.All` の付与は不要です。

- [チャット情報の取得に必要な権限](https://learn.microsoft.com/ja-jp/graph/api/chat-get?view=graph-rest-1.0)
- [メッセージ一覧と並べ替えの仕様](https://learn.microsoft.com/ja-jp/graph/api/chat-list-messages?view=graph-rest-1.0)
- [Microsoft Entra IDで管理者の同意を付与する手順](https://learn.microsoft.com/ja-jp/entra/identity/enterprise-apps/grant-admin-consent)
- [OAuth 2.0クライアント資格情報フロー](https://learn.microsoft.com/ja-jp/entra/identity-platform/v2-oauth2-client-creds-grant-flow)
- [Teamsのリソース固有の同意](https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/rsc/resource-specific-consent)

### 実行

組織で承認された1対1チャットのGraphチャットIDと、登録アプリの設定を環境変数で渡します。

| 環境変数 | 値 |
| --- | --- |
| `TEAMS_TENANT_ID` | Entraディレクトリ（テナント）ID |
| `TEAMS_CLIENT_ID` | Entraアプリケーション（クライアント）ID |
| `TEAMS_CLIENT_SECRET` | シークレットマネージャーから注入したシークレットの値 |
| `TEAMS_CHAT_ID` | 対象のGraphチャットID（URLエンコード前の値） |

```sh
mkdir -m 700 -p ./data
python3 -m teams_chat_import --db ./data/teams-chat.sqlite3
```

- Graphで対象IDとチャット種別 `oneOnOne` を照合します。照合が通ると、対象チャットのメッセージを送信日時の降順で取得し、期間開始まで `@odata.nextLink` をたどります。
- 実行開始時刻を基準にUTCで暦上の6か月をさかのぼります。対応する日がない月は月末を使用し、期間の両端を含めます。送信日時で判定します。
- ユーザーが送信した本文を保存します。HTML本文はプレーンテキストに変換します。添付データのダウンロードは行わず、本文もあるメッセージはそのテキストを保存します。本文のない投稿、削除済み投稿、システムイベントは保存対象外です。
- `teams_chat_messages` テーブルにチャットID、メッセージID、送信者ID、表示名（取得できる場合）、送信日時（UTC）、本文を保存します。主キーは `(chat_id, message_id)` です。同じメッセージを再取得した場合は既存行を保持します。
- APIエラー時は処理名とHTTPステータス、取得できた場合はリクエストIDを標準エラーに記録して終了コード1を返します。保存はページ単位で確定するため、途中で失敗した場合は原因を解消して同じDBに対して再実行できます。
- SQLiteは実行ユーザーに限定したディレクトリに配置してください。新規DBファイルはUnix系環境で権限 `0600` で作成されます。SQLiteファイルにはチャット本文が保存されるため、保管時の暗号化は配置先のストレージで行ってください。

### テスト

```sh
python3 -m unittest discover -s tests -v
```

モックのEntra・Graphレスポンスと一時SQLite DBで検証します。実際のTeamsデータや認証情報は不要です。
