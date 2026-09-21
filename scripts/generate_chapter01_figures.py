"""Regenerate the published illustrative figures for chapter one."""

import csv
from html import escape
from pathlib import Path
import xml.etree.ElementTree as ET


OUT = Path(__file__).resolve().parents[1] / "docs" / "chapters" / "chapter01-assets"
FONT = 'font-family="Yu Gothic,Meiryo,Noto Sans JP,sans-serif"'
TEACHER = "#2563eb"
STUDENT = "#b45309"


def text(x, y, value, size=16, **attributes):
    attributes = " ".join(f'{key.replace("_", "-")}="{escape(str(value))}"'
                          for key, value in attributes.items())
    return f'<text x="{x}" y="{y}" font-size="{size}" {attributes}>{escape(value)}</text>'


def save(name, parts, width, height, title, description):
    document = "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        f"<title id=\"title\">{escape(title)}</title>",
        f"<desc id=\"desc\">{escape(description)}</desc>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<g {FONT} fill="#172033">',
        *parts,
        "</g></svg>",
    ])
    ET.fromstring(document)
    (OUT / name).write_text(document + "\n", encoding="utf-8", newline="\n")


def chart(name, title, subtitle, xs, teacher, student, x_label, y_max, cross, note):
    width, height = 720, 460
    left, top, plot_width, plot_height = 92, 115, 590, 235
    x_max = xs[-1]
    def point(x, y):
        return left + x / x_max * plot_width, top + plot_height - y / y_max * plot_height
    parts = [
        text(24, 32, title, 23, font_weight="bold"),
        text(24, 61, subtitle, 15),
        f'<line x1="28" x2="68" y1="86" y2="86" stroke="{TEACHER}" stroke-width="3"/>',
        text(78, 92, "教師モデル", 16),
        f'<line x1="305" x2="345" y1="86" y2="86" stroke="{STUDENT}" stroke-width="3" stroke-dasharray="8,5"/>',
        text(355, 92, "学習済みの生徒モデル", 16),
    ]
    for i in range(5):
        y = y_max * i / 4
        _, py = point(0, y)
        parts.extend([
            f'<line x1="{left}" x2="{left+plot_width}" y1="{py}" y2="{py}" stroke="#d9e0e7"/>',
            text(left - 10, py + 5, f"{y:,.0f}", 14, text_anchor="end"),
        ])
    for x in xs:
        px, _ = point(x, 0)
        parts.append(text(px, top + plot_height + 27, f"{x:,}", 14, text_anchor="middle"))
    parts.append(text(left, top - 8, "費用（円）", 14))
    parts.append(text(left + plot_width / 2, 407, x_label, 16, text_anchor="middle"))
    for values, color, dashed in ((teacher, TEACHER, ""), (student, STUDENT, 'stroke-dasharray="8,5"')):
        points = " ".join(f"{px:.2f},{py:.2f}" for px, py in
                          (point(x, y) for x, y in zip(xs, values)))
        parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="3" {dashed}/>')
    cx, cy = point(*cross)
    parts.extend([
        f'<line x1="{cx}" x2="{cx}" y1="{cy}" y2="{top+plot_height}" stroke="#64748b" stroke-dasharray="3,4"/>',
        f'<circle cx="{cx}" cy="{cy}" r="6" fill="#fff" stroke="#172033" stroke-width="2"/>',
        f'<rect x="{cx+12}" y="{cy-48}" width="186" height="36" rx="5" fill="#f1f5f9"/>',
        text(cx + 22, cy - 24, note, 15, font_weight="bold"),
        text(24, 443, "説明用の仮想値です。Azureの価格・実測結果・品質同等性を示しません。", 13),
    ])
    save(name, parts, width, height, title, subtitle + "。" + note)


def flow():
    nodes = [
        ("1. 業務条件と評価基準を決める", "何を正解とし、何を許さないか"),
        ("2. 教師モデルの履歴を収集・選別する", "学習用と評価用のデータを分ける"),
        ("3. 学習前の生徒モデルを測る", "評価①：保存済み履歴から次の行動"),
        ("4. 生徒モデルへ追加学習する", "教師ありファインチューニング（SFT）"),
        ("5. 学習後も同じ評価①で比べる", "改善と悪化を、条件を揃えて確認"),
        ("6. 3者で業務全体を比べる", "評価②：仕事の完了・違反・待ち時間・使用量"),
        ("7. 品質・時間・総費用から判断する", "採用／条件付き採用／見送り／保留"),
    ]
    parts = [
        '<defs><marker id="arrow" markerWidth="10" markerHeight="8" refX="8" refY="4" orient="auto">'
        '<path d="M0,0 L8,4 L0,8 Z" fill="#64748b"/></marker></defs>',
    ]
    for index, (title, subtitle) in enumerate(nodes):
        y = 12 + index * 92
        if index:
            parts.append(f'<line x1="290" x2="290" y1="{y-22}" y2="{y-5}" '
                         'stroke="#64748b" stroke-width="2" marker-end="url(#arrow)"/>')
        parts.extend([
            f'<rect x="20" y="{y}" width="540" height="70" rx="9" fill="#eff6ff" stroke="#93b5e7"/>',
            text(290, y + 28, title, 19, text_anchor="middle", font_weight="bold"),
            text(290, y + 52, subtitle, 16, text_anchor="middle"),
        ])
    save("flow.svg", parts, 580, 650, "蒸留と評価の実践の流れ",
         "業務定義、教師履歴、学習前評価、追加学習、学習後評価、業務全体評価、採用判断の7段階。")


def main():
    OUT.mkdir(exist_ok=True)
    xs = [0, 250, 500, 750, 1000]
    teacher = [20 * x for x in xs]
    student = [9000 + 2 * x for x in xs]
    assert teacher[2] == student[2] == 10000
    chart("monthly-cost.svg", "図1：どれくらい使うと月額が安くなるか",
          "月額運用費の比較。初期費用は含めません。",
          xs, teacher, student, "1か月の問い合わせ件数", 20000,
          (500, 10000), "月500件で同額")
    months = list(range(7))
    cumulative_teacher = [20000 * month for month in months]
    cumulative_student = [18000 + 11000 * month for month in months]
    assert cumulative_teacher[2] == cumulative_student[2] == 40000
    chart("payback.svg", "図2：追加の初期費用をいつ回収できるか",
          "月1,000件で一定と仮定。生徒側に追加初期費用18,000円。",
          months, cumulative_teacher, cumulative_student, "運用開始からの月数", 120000,
          (2, 40000), "2か月で同額")
    with (OUT / "illustrative-costs.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["monthly_requests", "teacher_monthly_jpy", "trained_student_monthly_jpy"])
        writer.writerows(zip(xs, teacher, student))
    flow()
    print("Generated three SVG figures and illustrative-costs.csv; crossover checks passed.")


if __name__ == "__main__":
    main()
