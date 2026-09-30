.PHONY: demo test seed clean

demo:
	.venv/bin/python demo.py

test:
	.venv/bin/python -m unittest discover -s tests -v

seed:
	.venv/bin/python -m reconpilot.seed.generate

clean:
	rm -rf data/*.db data/psp_files data/withheld data/orders.csv reports/* models/*
