# GASコード生成ルール

Step 5でGASコードを生成するときは、必ずこのルールとテンプレートに従うこと。

## 生成の絶対ルール

1. **コードに生データの値を埋め込まない。** コードに書いてよいのは列名・手法・パラメータ（＝仕様書の内容）のみ。「山田太郎」のような実際の値を定数や例として書いてはいけない。
2. **列の特定はヘッダー名で行う。** 列番号の直書きは列順の変化に弱いため、ヘッダー行から列名でインデックスを引く方式にする。
3. **元データは変更しない。** 出力は常に「anonymized」シートへ（既存なら削除して作り直し）。元シートは読み取りのみ。
4. **列名の変更まで実装する。** 一般化した列は中身だけでなく列名も変える（住所→市区町村、生年月日→年代）。匿名化済みであることがヘッダーだけで判別でき、再添付時の検収が機械的にできるようにするため。
5. **冒頭コメントに仕様書の要約を入れる。** 利用目的・生成日・列ごとの処理一覧を記載し、コードと仕様書が1対1で監査できるようにする。

## コードの骨格

```javascript
/**
 * 個人情報匿名化スクリプト（pii-anonymization-advisor 自動生成）
 * 利用目的 : {{目的}}
 * 生成日   : {{日付}}
 * 処理内容 : {{列→手法の一覧}}
 */
function anonymize() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const src = ss.getSheets()[0];                       // 元データ（1枚目のシート）
  const data = src.getDataRange().getValues();
  const header = data[0].map(String);
  const col = name => header.indexOf(name);            // 列名→インデックス

  // ---- 仕様書に基づく設定（ここだけが仕様ごとに変わる）----
  const DROP    = [/* 削除する列名 */];                 // 例: "氏名","メールアドレス"
  const RENAME  = {/* "住所":"市区町村" など */};
  // -------------------------------------------------------

  const rows = data.slice(1).map(row => {
    const r = row.slice();
    // {{列ごとの変換処理をここに生成}}
    return r;
  });

  // 列削除と列名変更を適用して出力
  const keep = header.map((h, i) => DROP.includes(h) ? -1 : i).filter(i => i >= 0);
  const outHeader = keep.map(i => RENAME[header[i]] || header[i]);
  const outRows = rows.map(r => keep.map(i => r[i]));

  let out = ss.getSheetByName('anonymized');
  if (out) ss.deleteSheet(out);
  out = ss.insertSheet('anonymized');
  out.getRange(1, 1, outRows.length + 1, outHeader.length)
     .setValues([outHeader, ...outRows]);
}
```

## 手法→変換処理のパーツ（仕様書の行ごとに組み合わせる）

**削除** … DROP配列に列名を追加するだけ（変換処理は不要）。

**一般化：住所→市区町村**
```javascript
r[col('住所')] = String(r[col('住所')]).match(/^(.{2,3}?[都道府県])?(.+?[市区町村郡])/)?.[0] || '（不明）';
// RENAME に "住所":"市区町村" を追加
```

**一般化：生年月日→年代（10歳刻み）**
```javascript
const age = Math.floor((Date.now() - new Date(r[col('生年月日')])) / 3.15576e10);
r[col('生年月日')] = isNaN(age) ? '（不明）' : Math.floor(age / 10) * 10 + '代';
// RENAME に "生年月日":"年代" を追加
```

**マスキング（例：氏名の名を伏せる）**
```javascript
r[col('氏名')] = String(r[col('氏名')]).replace(/^(\S+)\s*.*$/, '$1 ＊＊');
```

**仮名化（連番置換＋対応表）**
```javascript
// mappingシートに対応表を出力する。対応表は元データと同等の機密。
// 生成時は必ず「mappingシートは別ファイルに移して共有範囲を限定してください」と警告を添える。
```

**ハッシュ化（突合キー用・SHA-256）**
```javascript
const bytes = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, String(r[col('メールアドレス')]));
r[col('メールアドレス')] = bytes.map(b => ('0' + (b & 0xFF).toString(16)).slice(-2)).join('');
```

**ダミーデータ生成（テストデータ目的）** … 変換ではなく全面置換。列ごとに架空値（連番名・example.comメール・実在しない電話番号帯）を生成するコードを書く。

## コードと一緒に必ず添える案内文

> このコードを、元データのスプレッドシートの「拡張機能 → Apps Script」に貼り付けて、▶（実行）を押してください。初回のみGoogleの承認画面が出ます（自分のアカウント内の許可です）。実行が終わると「anonymized」シートができます。**そのシートをCSV等でこのチャットに再添付**してください。元データのシートは送らないでください。

## 再添付後の検収（Step 6）

1. 再添付ファイルのヘッダーを確認：氏名・フリガナ・メールアドレス・電話・住所・生年月日など個人情報を示す列名が**残っていないこと**
2. 列構成が仕様書の「そのまま」＋変更後列名（市区町村・年代など）と一致すること
3. 問題なければ最初の依頼（集計など）を実行。残っていれば理由を列名だけで伝え、再実行を依頼する
