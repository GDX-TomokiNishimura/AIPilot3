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

## ナレッジ管理DB

`knowledge_db.KnowledgeStore` は会話、業務知識、判断基準を扱うバックエンド用のPython APIです。既存のGraphバッチと同じSQLiteファイルを指定できます。初回に必要なテーブルを追加し、既存の `teams_chat_messages` の行をそのまま読み込みます。

```python
from datetime import UTC, datetime

from knowledge_db import KnowledgeStore

store = KnowledgeStore("./data/teams-chat.sqlite3")
store.register_message(
    chat_id="dummy-chat",
    message_id="message-1",
    sender_id="dummy-mentor",
    sent_at=datetime(2026, 8, 10, 6, 30, tzinfo=UTC),
    body="発注前に在庫数を確認してください。",
    source="manual",
    source_ref="dummy-notes:1",
)
matches = store.search_messages("在庫", chat_id="dummy-chat")
candidate = store.submit_criterion(
    statement="発注前に在庫を確認する",
    reason="重複発注を防ぐため",
    evidence=[(message.chat_id, message.message_id) for message in matches],
)
store.approve_criterion(
    candidate.criterion_id, expected_revision=candidate.revision, actor="dummy-reviewer",
)
official = store.approved_criteria()
```

### 会話とナレッジ

- `register_message()` は整形済みのメッセージを登録します。取得元は `graph`、`purview`、`manual` のいずれかで、`source_ref` には元データのURLやレコード識別子を指定できます。Graphバッチはレスポンスに `webUrl` がある場合に保存します。出典の記録がない既存行は `graph` として扱います。
- 会話IDとメッセージIDが両方一致する場合は、登録済みの本文を保持したまま出典を追加できます。各出典には最初の空でない参照を保持します。将来のPurview取り込みでは、Graphと同じメッセージを共通のIDに対応付ける必要があります。
- `search_messages()` は本文の部分一致で検索し、会話IDと取得元で絞り込めます。
- `add_knowledge()` と `list_knowledge()` で、業務知識（`business_knowledge`）、業務ルール（`business_rule`）、プロジェクト情報（`project`）、指導内容（`employee_guidance`）、習熟度（`employee_proficiency`）を扱えます。対象の社員やプロジェクトは `subject_ref` で識別できます。

### 判断基準のレビュー

- `submit_criterion()` は判断基準、理由、根拠となる会話IDとメッセージIDを保存します。理由が空なら確認待ち（`awaiting_reason`）、理由があれば承認待ち（`pending`）になります。
- `revise_criterion()` は新しい版を追加します。レビュー担当者は `approve_criterion()` または `reject_criterion()` で対象の版を処理します。承認には理由が必要です。呼び出し元がレビュー担当者の権限を確認し、本人の識別子を `actor` に渡します。
- AIが正式な判断根拠を取得する場合は `approved_criteria()` を使います。判断基準ごとに最新の承認済み版を取得します。候補の登録、修正、却下で、過去に承認された版が自動的に置き換わることはありません。
- `criterion_history()` では登録・修正・承認・却下の操作、担当者、日時を確認できます。`get_criterion(..., revision=版番号)` で過去の本文、理由、根拠メッセージ、承認日時を参照できます。

SQLite固有の処理は `knowledge_db.py` と `conversation_storage.py` の永続化層にまとめています。クラウドDBへ移行する際は、呼び出し側が使うメソッドとデータ型を維持しながら永続化層を差し替えられます。Purviewエクスポートの解析は後続Issueの対象です。

## ナレッジ管理Web画面

Python 3.11以上で、ダミーデータを使って管理画面を起動できます。

```sh
python3 -m knowledge_admin --demo
```

ブラウザで [ローカル管理画面](http://127.0.0.1:8080/) を開いてください。ダッシュボードから判断基準の確認・修正・承認・却下や、業務知識の閲覧ができます。デモの変更は一時SQLiteファイルに保存され、サーバーの終了時に破棄されます。

既存の開発用SQLiteを使う場合は `--db ./data/teams-chat.sqlite3` を指定します。ポートの変更には `--port` を使います。サーバーは常に `127.0.0.1` で待ち受ける認証未実装のローカル用です。接続元のホスト名と、指定された場合はOriginを確認します。

画面は同一オリジンのJSON APIを使用します。APIは `KnowledgeStore` を通じて変更を保存し、古い版へのレビューを拒否します。AI向けデータは引き続き `KnowledgeStore.approved_criteria()` から取得します。将来のMicrosoft 365 SSO連携では、`AdminServer` の `reviewer_for_request` に認証と権限確認を行う処理を接続し、認証済みの利用者IDをレビュー履歴に渡します。

```sh
python3 -m unittest discover -s tests -v
```

Webのテストはローカルの一時ポートと一時SQLiteを使います。
