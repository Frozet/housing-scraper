import json

import pytest

from app import collect


@pytest.mark.parametrize("fixture_name", ["auction_listing", "review_listing", "closed_listing"])
def test_import_of_excluded_listing_does_not_write_to_database(
        request, fixture_name, tmp_path, monkeypatch, capsys):
    html, url = request.getfixturevalue(fixture_name)
    source = tmp_path / "listing.html"
    source.write_text(html)
    monkeypatch.setattr("sys.argv", ["collect", "--html", str(source), "--url", url])
    def unexpected_session():
        pytest.fail("Пропуск объявления не должен начинать запись в базу")
    monkeypatch.setattr(collect, "SessionLocal", unexpected_session)
    collect.main()
    result = json.loads(capsys.readouterr().out)
    assert result["сохранено"] is False and "Сбор продолжается" in result["причина"]
