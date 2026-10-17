.PHONY: sync test
sync:
	python -m health_sync run garmin-activities-to-sheet
test:
	python -m unittest discover -s tests
