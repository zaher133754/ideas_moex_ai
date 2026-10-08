from datetime import timedelta

from scout.models import Report
from scout.ranking import rank
from scout.reports import export, html_report, summary
from scout.storage import Storage


def make_report(idea, quote, verdict, settings, now):
    ranked, _ = rank(idea, verdict, [quote], settings, now, [])
    return Report(
        day=now.date().isoformat(),
        generated_at=now,
        market_context="Тестовый контекст",
        ideas=[ranked],
        rejected=[],
        notes=[],
    )


def test_seen_only_after_confirmed_delivery(idea, quote, verdict, settings, now):
    storage = Storage(settings.data_dir)
    report = make_report(idea, quote, verdict, settings, now)
    storage.save_report(report)
    assert not storage.history(now, 90)
    storage.delivery_state(report.day, "uncertain")
    assert not storage.history(now, 90)
    storage.delivered(report, 123, now)
    assert storage.delivery(report.day) == "sent"
    assert len(storage.history(now, 90)) == 1
    assert not storage.history(now + timedelta(days=91), 90)
    assert storage.report(report.day) == report


def test_html_escapes_agent_text_and_has_links(idea, quote, verdict, settings, now):
    report = make_report(idea, quote, verdict, settings, now)
    report.market_context = '<script>alert("owned")</script>'
    html = html_report(report, 10)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert '<a href="https://issuer.example/news/report"' in html
    assert "<table>" in html
    assert '<meta charset="utf-8">' in html


def test_summary_fits_telegram_even_with_extreme_multi_leg_sources(idea, quote, verdict, settings, now):
    report = make_report(idea, quote, verdict, settings, now)
    report.ideas *= 3
    for item in report.ideas:
        item.idea.title = "😀" * 150
        item.idea.sources[0].url = "https://example.com/" + "long" * 1000
    text = summary(report)
    assert len(text.encode("utf-16-le")) // 2 <= 4096


def test_export_both_sizes_and_empty_report(settings, now):
    report = Report(
        day=now.date().isoformat(),
        generated_at=now,
        market_context="",
        ideas=[],
        rejected=[{"title": "Старый тезис", "reason": "Истёк срок"}],
        notes=[],
    )
    paths = export(report, settings.data_dir / "reports")
    assert len(paths) == 5
    assert all(p.exists() for p in paths.values())
    assert "нет" in paths["10.html"].read_text(encoding="utf-8")
    assert "Топ 0" in summary(report)
