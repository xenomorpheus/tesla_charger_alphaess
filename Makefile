all: env pycodestyle autoformat lint mypy docs

.PHONY: env docs all

env:
	@echo "===== Environment ====="
	@/bin/echo -n "Python Interpreter: "; which python3
	@echo "PYTHONPATH=${PYTHONPATH}"
	@echo "PATH=${PATH}"
	@echo ""
	@echo ""

pycodestyle:
	@echo "===== pycodestyle ====="
	pycodestyle --max-line-length=120 *.py

autoformat:
	@echo "===== autoformat ====="
	black --line-length=120 *.py

lint:
	@echo "===== Lint ====="
	pylint --exit-zero *.py

mypy:
	@echo "===== MyPy ====="
	mypy *.py

docs:
	mkdir -p docs
	rm -fr docs/
	pdoc3 --force --html *.py

clean:
	rm -r __cache__

pipupdate:
	pip --disable-pip-version-check list --outdated --format=json | python -c "import json, sys; print('\n'.join([x['name'] for x in json.load(sys.stdin)]))" | xargs -n1 pip install -U
