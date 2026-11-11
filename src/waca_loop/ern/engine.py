"""3エンジンと候補選定（SPEC_phase3 §2.3・§3・§4）.

出口は :func:`build_candidates` ひとつ。ここから外へ出るのは **band と evidence_type だけ**で、
専門度・貢献量の数値は一切出ない（§2.3 注）。

ACL の掛かり方（§4）:

- **Gate1（§4b）** — 集計対象の問いは、**質問者の可視範囲**を継承する。ERN 独自の可視範囲を
  持たない。呼び出し側（`ServerContext.find_expert`）が `knowledge.acl` を通した問いだけを渡す。
- **§4a** — 著者マスク該当行（E-3(ii)）は**集計前**に落とす。可視のまま著者名だけ伏せて
  配信されている行を実績に数えると、伏せたはずの著者が「その領域の専門家」として名前で出る。
- **Gate2（§4c）** — 名前を出してよいのは
  `viewer.is_org_member ∧ person.consent_internal ∧ person.status == 'active'` を**すべて**
  満たす人物だけ。判定は `retrieval.acl.person_visible_to` に委ねる（E11: ここに
  可視性のロジックを書かない）。**キャッシュを置かない** — opt-out が次の呼び出しで
  即時に効くため（E5）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from waca_loop.ern.signals import PersonSignals, aggregate
from waca_loop.ern.weights import (
    BAND_FRESH_OLD,
    BAND_FRESH_RECENT,
    BAND_TOPIC_STRONG,
    BAND_TOPIC_WEAK,
    EVIDENCE_ROLE,
    EVIDENCE_SIGNAL,
    FRESHNESS_RECENT_DAYS,
    TOPIC_MATCH_STRONG,
)
from waca_loop.knowledge.acl import mask_author
from waca_loop.models import Feedback, Person, Question, Viewer
from waca_loop.retrieval.acl import person_disclosable_as_expert

# 上限の数字はここに**置かない**。`server._K_CAPS["find_expert"]` が唯一の置き場所で、
# クランプは `_clamp_k` が行う（SPEC_phase3 §0「新しい上限機構を作らない」）。
# 既定値を持たせると同じ数が2箇所になり、片方だけ直る余地ができる — だから `limit` は
# **必須引数**にしてある。呼び出し側は必ずクランプ済みの値を渡すこと。


@dataclass(frozen=True)
class Candidate:
    """応答1件ぶん。**数値を持たない**（E1）."""

    person_id: str
    display_name: str
    units: tuple[str, ...]
    topic_match: str
    freshness: str
    declared_topics: tuple[str, ...]
    evidence_type: str

    def as_dict(self) -> dict:
        return {
            "person_id": self.person_id,
            "display_name": self.display_name,
            "units": list(self.units),
            "topic_match": self.topic_match,
            "freshness": self.freshness,
            "declared_topics": list(self.declared_topics),
            "evidence_type": self.evidence_type,
        }


def _normalize(text: str) -> str:
    return (text or "").strip().lower()


#: 監査に残してよい領域ラベルの上限。用語集の語（部署名・制度名・略語など）が収まり、
#: 問い合わせ文は収まらない長さとして 24 字を取る。
TOPIC_LABEL_MAX = 24

#: 文の徴候。これを含むものは**ラベルではなく文**である。用語集の語は句読点・疑問符・
#: 感嘆符を含まない。
#: （空白は入れない — 空白を含む英語の語（例: "Single Source of Truth"）がありうる）
_SENTENCE_MARKS = "、。？?！!"


def audit_topic(topic: str) -> str | None:
    """監査行に残してよい形の `topic`、または残せないなら None（INV-1 / E8）.

    `find_expert(topic)` は MCP クライアントから**そのまま**来る。DDL は
    「探した領域ラベル。**問い合わせ本文ではない**」と書いているが、それを守らせるのが
    この関数である。

    実際に来うるもの:

        find_expert("山田さんが担当していた○○社の解約案件の件で、法務に詳しい人")

    これがそのまま `<DS_FEEDBACK>.ern_audit.topic` に**永久に**残ると、人名・取引先名・
    商談の事実を書き込んだことになる。`search_sfaq` はクエリを1文字も保存していないので、
    ERN だけが呼び出し側の文字列を表に書くツールになってしまう。

    見るのは2つ:

    - **長さ** — 用語集の語が収まる上限として 24 字。
    - **文の徴候** — 用語集の語は `、。？?！!` を含まない。含むなら文である。
      長さだけでは足りない: 上の例は 30 字だが、人名や相談の内容を含んだまま 24 字以内に
      収まる文はいくらでも書ける。

    切り詰めない — 文を途中で切ると、断片が残るうえに「ラベルである」という嘘が付く。
    残せないものは **None**（監査行は残るが topic は空）にして、「探されたことは記録され、
    何を探したかは残らない」という、E6 と矛盾しない状態にする。

    **これは仕掛けであって、意図的な迂回を止めるものではない** — 24 字以内で句読点の無い
    文は書ける（`evidence_leak` や `_TOOL_SELF_TERMS` と同じ性質）。取りこぼしたぶんは、
    領域ラベルを統制語彙へ解決する形で塞ぐのが本筋で、それは Open item（SPEC_phase3 §7）。
    """
    label = _normalize(topic)
    if not label or len(label) > TOPIC_LABEL_MAX:
        return None
    if any(mark in label for mark in _SENTENCE_MARKS):
        return None
    return label


def question_matches_topic(question: Question, topic: str) -> bool:
    """その問いが領域 ``topic`` に紐づくか（リコール）.

    判定は2つ: `attributes.domain` に載っているか、問い本文に語が現れるか。
    **ベクトル距離は使わない** — `ern_assertions.embedding` はリコール専用で、
    スコアには一切寄与させない（"no vectors for scoring"）。ここで距離を
    足すと、それが実質の順位付けになる。
    """
    t = _normalize(topic)
    if not t:
        return False
    domains = (question.attributes or {}).get("domain") or []
    if isinstance(domains, (list, tuple)):
        if any(t == _normalize(str(d)) for d in domains):
            return True
    return t in _normalize(question.question_text)


def routable_feedback(
    questions: list[Question],
    feedback_by_question: dict[str, list[Feedback]],
    topic: str,
) -> list[Feedback]:
    """ルーティング原料 — 領域に紐づき、かつ**著者マスク該当でない**問いのフィードバック.

    §4a はここ。`mask_author` が真の問いは「著者を伏せる」約束で配信されている
    （SPEC_phase2 §2.3 E-3(ii)）ので、その行を専門度の材料にしてはならない。
    落とすのは**集計前**である必要がある — 集計してから候補段で弾いても、その人の
    シグナルは他の候補の相対位置を通じて残る。
    """
    out: list[Feedback] = []
    for q in questions:
        if not question_matches_topic(q, topic):
            continue
        if mask_author(_question_row(q)):
            continue
        out.extend(feedback_by_question.get(q.question_id, []))
    return out


def _question_row(q: Question) -> dict:
    """`mask_author` は行 dict を見る（SQL 側と同じ形）。dataclass から必要分だけ写す."""
    return {
        "provenance": q.provenance.value if q.provenance is not None else None,
        "perm_corporate": q.perm_corporate,
        "perm_individual": q.perm_individual,
    }


def _topic_band(expertise: float) -> str:
    return BAND_TOPIC_STRONG if expertise >= TOPIC_MATCH_STRONG else BAND_TOPIC_WEAK


def _freshness_band(days: float | None) -> str:
    if days is None:
        return BAND_FRESH_OLD
    return BAND_FRESH_RECENT if days <= FRESHNESS_RECENT_DAYS else BAND_FRESH_OLD


def build_candidates(
    *,
    viewer: Viewer,
    topic: str,
    questions: list[Question],
    feedback_by_question: dict[str, list[Feedback]],
    people: dict[str, Person],
    declared: dict[str, list[str]],
    half_life_days: float,
    now: datetime,
    limit: int,
    role_fallback: list[str] | None = None,
) -> list[Candidate]:
    """候補（最大 ``limit`` 名）。**person_id 昇順の固定ソート**で返す.

    並びが「良い順」を意味しないことを、構造で保証する（E2）。内部では専門度で上位を
    選ぶが、**返す前に person_id で並べ直して選定順を破壊する**。並びから順位が読めると、
    数値を出していなくても実質のランキングになる。

    ``role_fallback`` はシグナルがゼロだったときの役職ベースの候補（E10）。
    実績で埋まったぶんは実績ベース、足りないぶんだけ役職ベースで補い、
    `evidence_type` を**候補ごとに**正しく付ける（混在する）。
    """
    if not viewer.is_org_member:
        return []          # 非 org メンバーは常に空（fail closed・§4c）

    signals = aggregate(
        routable_feedback(questions, feedback_by_question, topic),
        half_life_days=half_life_days,
        now=now,
    )

    def disclosable(person_id: str) -> Person | None:
        """Gate2（§4c）。**毎回引き直す** — キャッシュを置くと opt-out が次の呼び出しで
        効かない（E5）。

        `person_visible_to` ではなく `person_disclosable_as_expert` を使う。前者には
        「本人は自分を見られる」例外があり、それを ERN に持ち込むと、オプトアウトした人が
        自分で検索したときだけ自分が候補として返る — 降ろしたはずの機能が本人にだけ
        動き続ける（§4c は3条件を「すべて」と書いている）。
        """
        person = people.get(person_id)
        if person is None or not person_disclosable_as_expert(person, viewer):
            return None
        return person

    # 実績ベース: 専門度の高い順に選ぶ（**選定の内部順であって、出力順ではない**）。
    # 反証で負に振れた人は候補にしない — 0 を境目にするのは「実績がある」の最低条件。
    ranked: list[PersonSignals] = sorted(
        (s for s in signals.values() if s.expertise > 0.0),
        key=lambda s: (-s.expertise, s.person_id),
    )

    picked: list[Candidate] = []
    seen: set[str] = set()
    for sig in ranked:
        if len(picked) >= limit:
            break
        person = disclosable(sig.person_id)
        if person is None:
            continue
        seen.add(sig.person_id)
        picked.append(_candidate(person, declared, EVIDENCE_SIGNAL,
                                 _topic_band(sig.expertise),
                                 _freshness_band(sig.days_since_latest), topic))

    # 役職ベースのフォールバック（コールドスタート・E10）。シグナルが無い領域でも
    # 「誰に聞けばよいか」を返せるようにする。帯は実績のそれと取り違えないよう、
    # 弱い側に固定する。
    for person_id in role_fallback or []:
        if len(picked) >= limit:
            break
        if person_id in seen:
            continue
        person = disclosable(person_id)
        if person is None:
            continue
        seen.add(person_id)
        picked.append(_candidate(person, declared, EVIDENCE_ROLE,
                                 BAND_TOPIC_WEAK, BAND_FRESH_OLD, topic))

    # E2: 内部選定順を破壊してから返す。
    return sorted(picked, key=lambda c: c.person_id)


def _candidate(
    person: Person, declared: dict[str, list[str]], evidence_type: str,
    topic_match: str, freshness: str, topic: str,
) -> Candidate:
    return Candidate(
        person_id=person.person_id,
        display_name=person.display_name,
        units=tuple(sorted(person.unit_path or ())),
        topic_match=topic_match,
        freshness=freshness,
        declared_topics=_declared_for_topic(declared, person.person_id, topic),
        evidence_type=evidence_type,
    )


def _declared_for_topic(
    declared: dict[str, list[str]], person_id: str, topic: str
) -> tuple[str, ...]:
    """その人が**この領域を**宣言しているかどうかだけを返す（0件 or 1件）.

    宣言の全件は返さない。それは `find_expert` を**プロファイル列挙の道具**に
    してしまう — 1回の呼び出しで、同意している任意の会員について「役員報酬」「解雇手続」
    まで含む宣言一覧が読めてしまう。

    宣言そのものに ACL は無く（`ern_assertions` は3軸を持たず、`persons` の同意ゲートに
    従属する）、「エキスパート探索に出てよい」と言った人が「自分の宣言を全部一覧に
    されてよい」と言ったことにはならない。SPEC_phase3 §3 は `declared_topics` を
    無限定に書いているが、無限定に**返してよい**とは書いていない。狭い側に倒す。
    """
    t = _normalize(topic)
    return tuple(
        sorted({d for d in declared.get(person_id, ()) if _normalize(d) == t})
    )
