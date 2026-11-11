"""4D 出力規約・署名型 (§6).

``signature/prompt.py`` supplies the fixed system-prompt text that makes every client
(1) call ``log_usage`` per prompt, (2) resolve org terms via ``lookup_terms``,
(3) prefix a delegation tag, (4) add an interpretation note when ambiguous, and
(5) append the Diligence/Discernment signature line. Kept verbatim in Japanese.
"""

from __future__ import annotations

# Delegation tags — 委任タグ (§6 rule 3).
DELEGATION_AI = "[AI完結]"
DELEGATION_HUMAN = "[要人間判断]"

# Trailing signature — Diligence / Discernment (§6 rule 5).
SIGNATURE_LINE = (
    "— この回答はAIが下書きしました。数値・固有名詞は未検証です。"
    "利用者が確認・修正のうえ確定してください。"
)

# The fixed block injected into the client's system prompt (§6).
SYSTEM_PROMPT_RULES = f"""\
[WACA Loop 出力規約 / 4D署名]
1. ユーザ入力ごとに、必ず log_usage を呼ぶ（use_category, data_kinds, summary を生成して送る）。
   逐語の生プロンプトは送らず、要点要約のみを送ること。
   use_category は l2（そのまま採用）/ l1（条件つき）/ l0（いまは使わない）/ prohibited
   （禁止列挙に該当）から選ぶ。
   data_kinds は pii / confidential / internal / public に加え、コア資産の丸ごと持ち出しに
   相当するなら core_asset、最終判断（採点・認定・契約等）の委任に相当するなら
   final_judgment を含めること。get_applicable_policy で該当したポリシー行があれば、
   その row_id を policy_row_ids で渡すこと。応答に floor_warnings（下限・迂回路）や
   mismatch、coverage_hint（記録の取りこぼし）が返ったら、その内容をユーザに一行で伝える
   こと（いずれも助言であり利用は止めない）。
2. 組織固有の語・人名・略語が出たら lookup_terms で解決してから答える。
   判断・方針・経緯（なぜそう決めたか）に関する質問は search_sfaq で過去の判断を参照し、
   使ったナレッジの status（暫定/係争中）を回答に明示する。会話で価値ある判断が確認できたら、
   復唱→確認→感謝の流れで add_sfaq_feedback / add_sfaq_question に記録する（フィードバックB）。
3. 出力の冒頭に委任タグを付ける（AI単独で完結なら {DELEGATION_AI} / 人間の判断を要するなら {DELEGATION_HUMAN}）。
4. 解釈に曖昧さがあれば「〜の意味で解釈しました」を一行添える（Description）。
5. 出力末尾に必ず次の署名を付ける（Diligence/Discernment）：
   {SIGNATURE_LINE}
6. 添付・貼付データに個人情報の気配（氏名・メール・電話・住所・生年月日・カード番号等）が
   あれば、依頼された処理を始める前に必ず get_pii_anonymization_guidance を**列名のみ**で
   呼び、一旦止まって利用目的を確認し、匿名化仕様の承認を得てから進める。実際の値を
   引用・送信してはならない（下限 FLOOR-01）。
7. 読みやすさも規約のうち。上の1〜6は守ったうえで：
   ・結論を先に、1〜2行で書く。根拠・条件・注意はその後に置く。
   ・4のDescriptionは、**複数の解釈が実際にありうるときだけ**付ける。語の意味を尋ねられた等、
     一義的な問いには付けない（毎回付けると本文が定型に埋もれる）。
   ・2のstatusの明示は**回答内で1回だけ**。複数の判断を引いたなら「いずれも暫定」のように
     まとめる。項目ごとに繰り返さない。
"""


def delegation_tag(requires_human: bool) -> str:
    """委任タグ (§6 rule 3)."""
    return DELEGATION_HUMAN if requires_human else DELEGATION_AI


def signature_line() -> str:
    """末尾署名 (§6 rule 5)."""
    return SIGNATURE_LINE


def has_signature(text: str) -> bool:
    """True iff ``text`` carries both a delegation tag and the trailing signature (T9)."""
    if not text:
        return False
    has_tag = DELEGATION_AI in text or DELEGATION_HUMAN in text
    return has_tag and SIGNATURE_LINE in text


def wrap_output(
    body: str,
    *,
    requires_human: bool = False,
    interpretation_note: str | None = None,
) -> str:
    """Wrap a model answer with the 4D delegation tag + optional note + signature (§6).

    Useful for server-side post-processing or tests; in normal operation the client
    produces these via :data:`SYSTEM_PROMPT_RULES`.
    """
    parts = [delegation_tag(requires_human)]
    if interpretation_note:
        note = interpretation_note.strip()
        if not note.endswith(("。", ".")):
            note += "の意味で解釈しました。"
        parts.append(note)
    parts.append(body.strip())
    parts.append(SIGNATURE_LINE)
    return "\n".join(parts)
