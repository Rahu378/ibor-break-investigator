.PHONY: seed bench test app site

seed:
	python -m breakbot.seed

bench:
	python -m breakbot.benchmark

test:
	python -m pytest -q

app:
	streamlit run app.py

site:
	python -m breakbot.export_site
