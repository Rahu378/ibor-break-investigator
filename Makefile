.PHONY: seed bench test app site pages

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

# the Pages site is served from the gh-pages branch, a copy of docs/
pages:
	git subtree split --prefix docs -b gh-pages-tmp
	git push -f origin gh-pages-tmp:gh-pages
	git branch -D gh-pages-tmp
