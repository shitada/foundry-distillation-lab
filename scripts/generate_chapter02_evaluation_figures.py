"""Draw the two complementary evaluation views without external dependencies."""

from html import escape
from pathlib import Path
import xml.etree.ElementTree as ET


OUT = Path(__file__).resolve().parents[1] / "docs" / "chapters" / "chapter02-assets"
COLORS = {
    "input": ("#f1f5f9", "#94a3b8"),
    "model": ("#eff6ff", "#60a5fa"),
    "tool": ("#fff7ed", "#f59e0b"),
    "score": ("#f5f3ff", "#a78bfa"),
}


def label(x, y, value, size=17, bold=False, anchor="middle", color="#172033"):
    weight = ' font-weight="bold"' if bold else ""
    return (f'<text x="{x}" y="{y}" font-size="{size}" text-anchor="{anchor}" '
            f'fill="{color}"{weight}>{escape(value)}</text>')


def box(x, y, width, height, kind, dashed=False):
    fill, stroke = COLORS[kind]
    dash = ' stroke-dasharray="7,5"' if dashed else ""
    return (f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="12" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"{dash}/>')


def arrow(points, color="#64748b", end=True):
    marker = ' marker-end="url(#arrow)"' if end else ""
    return (f'<polyline points="{points}" fill="none" stroke="{color}" '
            f'stroke-width="2.5" stroke-linejoin="round"{marker}/>')


def frame(number, title):
    return [
        '<rect x="24" y="12" width="84" height="32" rx="16" fill="#1d4ed8"/>',
        label(66, 34, number, 18, True, color="#fff"),
        label(124, 36, title, 24, True, anchor="start"),
    ]


def save(name, parts, height, title, description):
    document = "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" width="760" height="{height}" '
        f'viewBox="0 0 760 {height}" role="img" aria-labelledby="title desc">',
        f'<title id="title">{escape(title)}</title>',
        f'<desc id="desc">{escape(description)}</desc>',
        '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" '
        'orient="auto"><path d="M0,0 L7,4 L0,8 Z" fill="#64748b"/></marker></defs>',
        '<rect width="100%" height="100%" fill="#fff"/>',
        '<g font-family="Yu Gothic,Meiryo,Noto Sans JP,sans-serif">',
        *parts,
        "</g></svg>",
    ])
    ET.fromstring(document)
    (OUT / name).write_text(document + "\n", encoding="utf-8", newline="\n")


def next_action():
    p = frame("評価①", "用意された場面で「次の一手」を確かめる")
    p += [
        box(24, 62, 712, 132, "input"),
        label(380, 89, "モデルへ渡す：途中までの会話履歴", 19, True),
        label(380, 115, "顧客の依頼：「破損した商品を返品して、返金してほしい」", 16),
        '<rect x="46" y="129" width="318" height="34" rx="7" fill="#fff"/>',
        '<rect x="396" y="129" width="318" height="34" rx="7" fill="#fff"/>',
        label(205, 152, "注文・配送情報は確認済み", 16),
        label(555, 152, "返品条件も確認済み", 16),
        label(380, 182, "ここまでの情報は用意する。途中の手順はモデルに実行させない。", 14),
        arrow("380,195 380,224"),
        box(168, 232, 424, 76, "model"),
        label(380, 262, "評価対象モデル", 20, True),
        label(380, 288, "次の行動を1回生成", 17),
        arrow("380,309 380,334"),
        box(168, 342, 424, 84, "tool", dashed=True),
        label(380, 370, "ツールを呼ぶ指示", 19, True),
        label(380, 394, "例：返金額の計算を依頼", 16),
        label(380, 416, "対象の注文・商品などを指定", 15),
        arrow("380,427 380,452"),
        box(24, 460, 712, 70, "score"),
        label(380, 489, "採点：選んだツールと、渡す情報の適切さ", 19, True),
        label(380, 515, "業務ルールに照らして確認した参照例と比べる", 16),
        '<rect x="24" y="546" width="712" height="62" rx="10" fill="#fffbeb"/>',
        label(380, 572, "ツールは実行しない。この場面で評価を終える。", 18, True),
        label(380, 596, "次の問題は用意した別の履歴から。前の問題での誤りは持ち越さない。", 14),
    ]
    save("evaluation-next-action.svg", p, 624,
         "評価①：一つのモデルの次の一手を確かめる",
         "確認済みの注文・配送情報と返品条件を含む途中の履歴を、一つの評価対象モデルへ渡す。"
         "モデルが出したツール呼び出しの指示を採点するが、ツールは実行しない。"
         "次の評価問題は用意した別の履歴から始める。")


def workflow():
    p = frame("評価②", "最初の依頼から、対応全体を任せる")
    p += [
        box(24, 62, 712, 112, "input"),
        label(380, 89, "モデルへ渡す：顧客の最初の依頼", 19, True),
        label(380, 117, "「破損した商品を返品して、返金してほしい」", 17),
        label(380, 151, "業務側の準備：注文・在庫の初期状態と業務ルール", 16),
        arrow("110,175 110,223"),
        box(36, 231, 300, 124, "model"),
        label(186, 263, "評価対象モデル", 20, True),
        label(186, 293, "確認する・計算する・回答する", 16),
        label(186, 324, "自分が得た結果を使って判断", 16),
        box(424, 231, 300, 124, "tool"),
        label(574, 263, "業務ツールを実行", 20, True),
        label(574, 292, "注文照会・返品条件の確認", 16),
        label(574, 320, "金額計算・返金処理（模擬）", 16),
        arrow("278,231 278,206 574,206 574,223"),
        label(428, 196, "呼び出し指示", 15, True),
        arrow("574,356 574,398 230,398 230,363"),
        label(402, 389, "実行結果を受け取り、再び判断", 15, True),
        arrow("92,356 92,445"),
        label(105, 431, "回答を選んだら", 15, anchor="start"),
        box(24, 453, 712, 74, "model"),
        label(380, 483, "最終的な対応・回答", 20, True),
        label(380, 510, "実際に行った対応と、その結果を顧客へ説明", 16),
        arrow("380,528 380,553"),
        box(24, 562, 712, 78, "score"),
        label(380, 592, "採点：途中の操作と、最終的な対応", 19, True),
        label(380, 620, "途中の誤り・処理時間・全呼び出しの使用量も確認", 16),
        label(380, 665, "一つのモデルに一件の対応を任せ、開始から回答までを記録", 14),
        '<rect x="24" y="680" width="712" height="60" rx="10" fill="#fffbeb"/>',
        label(380, 706, "途中で正しい履歴に差し替えない。", 18, True),
        label(380, 729, "実際の判断とツールの結果が、次の判断へ引き継がれる。", 15),
    ]
    save("evaluation-workflow.svg", p, 756,
         "評価②：最初の依頼から対応全体を任せる",
         "問い合わせと業務上の初期状態から、一つの評価対象モデルが次の行動を選び、"
         "ツールを実際に動かして結果を受け取り、再び判断する。"
         "最後の回答までの操作、結果、時間、使用量を確認する。返金は模擬処理であり、"
         "途中で正しい履歴を与え直さない。")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    next_action()
    workflow()
    print("Generated two evaluation diagrams.")


if __name__ == "__main__":
    main()
