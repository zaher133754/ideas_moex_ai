import re
from html import escape
from pathlib import Path

from scout.models import RankedIdea, Report

KINDS = {"stock": "Акция", "bond": "Облигация", "fund": "Фонд", "future": "Фьючерс", "arbitrage": "Арбитраж"}
UNITS = {"RUB": "₽", "percent_of_face": "% номинала", "points": "пунктов"}


def number(value: float | None) -> str:
    return "не подтверждено" if value is None else f"{value:,.4f}".rstrip("0").rstrip(".").replace(",", " ")


def brief(ranked: RankedIdea, index: int) -> str:
    idea = ranked.idea
    lines = [
        f"{index}. {idea.title[:125]}",
        f"{ranked.score:g}/100 · {KINDS[idea.kind]} · {idea.horizon[:60]} · плечо {idea.leverage:g}×",
    ]
    for leg in idea.legs:
        direction = "ЛОНГ" if leg.direction == "buy" else "ШОРТ"
        text = f"{leg.ticker} {direction}: вход {number(leg.entry_low)}–{number(leg.entry_high)} {UNITS[leg.unit]}"
        if leg.stop is not None:
            text += f"; стоп {number(leg.stop)}"
        if leg.targets:
            text += f"; цель {number(leg.targets[0])}"
        lines.append(text)
    if ranked.rr is not None:
        lines.append(f"Доход/риск с комиссией: {ranked.rr:.2f}")
    if ranked.net_edge_bps is not None:
        lines.append(f"Расчётная премия после расходов: {ranked.net_edge_bps:.1f} б.п.")
    lines.extend(
        [idea.thesis[:220], "Условие: " + idea.entry_condition[:170], "Риск: " + idea.risks[0][:130]]
    )
    source = max(idea.sources, key=lambda s: s.published_at)
    lines.append(str(source.url))
    return "\n".join(lines)


def summary(report: Report) -> str:
    count = min(3, len(report.ideas))
    title = f"Идеи российского рынка · {report.day}\nТоп {count} · подготовлен {report.generated_at:%H:%M}"
    if not count:
        return (
            title
            + "\n\nСвежих идей, прошедших проверки, сегодня нет.\nПричины отсева — в расширенном отчёте."
        )
    parts = [
        title,
        *[brief(r, i) for i, r in enumerate(report.ideas[:3], 1)],
        "Рейтинг — качество идеи, не вероятность прибыли. Цены и время проверки — в файле.",
    ]
    text = "\n\n".join(parts)
    # Source URLs and multi-leg plans can be long; keep a complete, smaller summary if needed.
    if len(text.encode("utf-16-le")) // 2 > 4000:
        parts = [title]
        for i, r in enumerate(report.ideas[:3], 1):
            assets = ", ".join(x.ticker + (" лонг" if x.direction == "buy" else " шорт") for x in r.idea.legs)
            parts.append(
                f"{i}. {r.idea.title[:100]}\n{r.score:g}/100 · {assets}\n{r.idea.thesis[:180]}\nВход, стоп, цели и источники — в отчёте."
            )
        text = "\n\n".join(parts)
    return text


def markdown(report: Report, limit: int) -> str:
    ideas = report.ideas[:limit]
    lines = [
        f"# Идеи российского рынка — {report.day}",
        f"Подготовлен: {report.generated_at.isoformat()}. Найдено подходящих идей: {len(report.ideas)}.",
        f"Исследование завершено: {(report.researched_at or report.generated_at).isoformat()}.",
        "Оценка 0–100 отражает качество доказательств, логики и исполнения; это не вероятность прибыли.",
        "Уровни условные: перед входом нужны текущие котировки, доступность шорта и условия вашего брокера.",
        "## Рынок",
        report.market_context or "Подтверждённый контекст не получен.",
    ]
    for index, ranked in enumerate(ideas, 1):
        idea = ranked.idea
        lines.extend(
            [
                f"## {index}. {idea.title} — {ranked.score:g}/100",
                f"Тип: {KINDS[idea.kind]}. Горизонт: {idea.horizon}. Плечо: {idea.leverage:g}×.",
                f"Применима до: {idea.valid_until.isoformat()}.",
                "### Обоснование",
                idea.thesis,
                "### Вход и выход",
                idea.entry_condition,
                "Отмена: " + idea.invalidation,
                "| Инструмент | Сторона / лоты корзины | Вход | Стоп | Цели | Единицы |",
                "|---|---|---|---|---|---|",
            ]
        )
        for leg in idea.legs:
            lines.append(
                f"| {leg.ticker} / {leg.board} | {'Покупка' if leg.direction == 'buy' else 'Шорт'} / {leg.ratio:g} | {number(leg.entry_low)}–{number(leg.entry_high)} | {number(leg.stop)} | {', '.join(number(t) for t in leg.targets) or 'по правилу выхода'} | {UNITS[leg.unit]} |"
            )
        if ranked.rr is not None:
            lines.append(
                f"Доход/риск после заданной комиссии: {ranked.rr:.2f} (по худшему входу и ближайшей цели)."
            )
        lines.extend(
            [
                "### Котировки Мосбиржи",
                "Цены относятся к указанному времени; ISS может публиковать данные с задержкой.",
            ]
        )
        for q in ranked.quotes:
            lines.append(
                f"- [{q.ticker}]({q.source_url}): {number(q.price)}, время сделки/дневной сессии {q.price_at.isoformat()}, получено {q.fetched_at.isoformat()}; оборот {number(q.daily_turnover_rub)} ₽; лот {q.lot_size}; bid/ask {number(q.bid)}/{number(q.ask)}."
            )
            if q.board in {"TQCB", "TQOB"}:
                dirty = (
                    q.price * q.face_value / 100 + (q.accrued_interest or 0)
                    if q.face_value and q.accrued_interest is not None
                    else None
                )
                lines.append(
                    f"  Номинал {number(q.face_value)} ₽; НКД {number(q.accrued_interest)} ₽; стоимость бумаги с НКД {number(dirty)} ₽; поле доходности ISS {number(q.yield_pct)}%; дюрация ISS {number(q.duration_days)} дней; погашение {q.maturity or 'не подтверждено'}."
                )
            if q.board == "RFUD":
                lines.append(
                    f"  Первоначальное ГО ISS {number(q.initial_margin_rub)} ₽; рублёвая стоимость единицы цены на лот {number(q.rub_per_price_unit_per_lot)}; экспирация {q.expiry or 'не подтверждена'}."
                )
        if idea.bond:
            bond = idea.bond
            lines.extend(
                [
                    "### Параметры облигации",
                    f"Доходность из идеи: {number(bond.yield_to_maturity_pct)}%. Источник: {bond.yield_source_url or 'не указан'}.",
                    f"Погашение: {bond.maturity}; дюрация: {number(bond.duration_years)} лет; рейтинг: {bond.credit_rating}.",
                    bond.coupon_and_offer,
                    bond.accrued_interest_note,
                    "Выход: " + bond.exit_rule,
                ]
            )
        if idea.arbitrage:
            a = idea.arbitrage
            lines.extend(
                [
                    "### Арбитражная корзина",
                    a.calculation,
                    f"Капитал корзины: {number(a.capital_rub)} ₽; заявленный сценарный убыток: {number(a.max_loss_rub)} ₽.",
                    f"Валовая премия: {a.gross_edge_bps:g} б.п.; прочие расходы: {a.other_costs_bps:g} б.п.; после расходов и комиссии: {number(ranked.net_edge_bps)} б.п.",
                    a.execution_conditions,
                    a.borrow_and_margin,
                    "Премия и сценарный убыток — оценки агента, не гарантированный результат; необходимо повторить расчёт по исполнимым ценам брокера.",
                ]
            )
        lines.extend(
            [
                "### Обеспечение и условия",
                idea.margin_and_borrow,
                *["- " + value for value in idea.prerequisites],
                "### Риски",
                *["- " + risk for risk in idea.risks],
                "### Почему такая оценка",
                ranked.verdict.reasoning,
                *[f"- {name}: {value:g}" for name, value in ranked.components.items()],
                "Слабые места:",
                *["- " + w for w in ranked.verdict.weaknesses],
                "### Источники",
            ]
        )
        for s in idea.sources:
            lines.extend(
                [
                    f"- [{s.title}]({s.url}) — {s.publisher}; опубликовано {s.published_at.isoformat()}; проверено {s.checked_at.isoformat()}.",
                    "  Дата: " + s.date_evidence,
                    "  Подтверждает: " + s.supports,
                ]
            )
    if not ideas:
        lines.append("Сегодня нет свежих идей, прошедших проверки. Количество не дополняется искусственно.")
    lines.extend(
        [
            "## Что отсеяно",
            *[f"- {r['title']}: {r['reason']}" for r in report.rejected],
            "## Примечания исследования",
            *["- " + note for note in report.notes],
        ]
    )
    # Consecutive table rows must stay together for Markdown readers.
    paragraphs = []
    table = []
    for line in lines:
        if line.startswith("|"):
            table.append(line)
        else:
            if table:
                paragraphs.append("\n".join(table))
                table = []
            paragraphs.append(line)
    if table:
        paragraphs.append("\n".join(table))
    return "\n\n".join(paragraphs) + "\n"


def html_report(report: Report, limit: int) -> str:
    # Self-contained, no network, JavaScript or agent-controlled HTML.
    md = markdown(report, limit)
    rendered = []

    def inline(value):
        # Parse links from raw text, then escape both label and URL.
        pattern = r"\[([^\]]+)\]\((https?://[^\s)]+)\)|(https?://[^\s<>\]\)]+)"
        parts, previous = [], 0
        for match in re.finditer(pattern, value):
            parts.append(escape(value[previous : match.start()]))
            label = match.group(1) or match.group(3)
            url = match.group(2) or match.group(3)
            parts.append(f'<a href="{escape(url, quote=True)}" rel="noreferrer">{escape(label)}</a>')
            previous = match.end()
        parts.append(escape(value[previous:]))
        return "".join(parts)

    for paragraph in md.split("\n\n"):
        if paragraph.startswith("### "):
            rendered.append("<h3>" + escape(paragraph[4:]) + "</h3>")
        elif paragraph.startswith("## "):
            rendered.append("<h2>" + escape(paragraph[3:]) + "</h2>")
        elif paragraph.startswith("# "):
            rendered.append("<h1>" + escape(paragraph[2:]) + "</h1>")
        elif paragraph.startswith("|"):
            rows = paragraph.splitlines()
            header = "".join("<th>" + escape(c.strip()) + "</th>" for c in rows[0].strip("|").split("|"))
            body = "".join(
                "<tr>"
                + "".join("<td>" + escape(c.strip()) + "</td>" for c in row.strip("|").split("|"))
                + "</tr>"
                for row in rows[2:]
            )
            rendered.append(
                f'<div class="table"><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>'
            )
        else:
            rendered.append('<p style="white-space:pre-wrap">' + inline(paragraph) + "</p>")
    content = "".join(rendered)
    return f"""<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Идеи · {escape(report.day)}</title>
<style>body{{margin:0;background:#f4f3ef;color:#202d35;font:16px/1.65 system-ui,sans-serif}}main{{max-width:980px;margin:32px auto;padding:36px;background:white;border-top:5px solid #176b5b}}h1{{font-size:30px;line-height:1.25}}h2{{margin-top:40px;padding-top:24px;border-top:1px solid #d8dedb}}h3{{font-size:18px;color:#176b5b}}p{{overflow-wrap:anywhere}}a{{color:#176b5b}}.table{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #d8dedb}}th{{background:#eef4f1}}@media(max-width:600px){{main{{margin:0;padding:20px}}}}@media print{{body{{background:white}}main{{margin:0;padding:0}}h2{{break-after:avoid}}}}</style><main>{content}</main></html>"""


def export(report: Report, directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for size in (5, 10):
        for extension, renderer in (("html", html_report), ("md", markdown)):
            path = directory / f"ideas-{report.day}-top{size}.{extension}"
            path.write_text(renderer(report, size), encoding="utf-8")
            paths[f"{size}.{extension}"] = path
    path = directory / f"ideas-{report.day}.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    paths["json"] = path
    return paths
