import csv
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
EXPECTED={'simulation_runs.csv': (['simulation_id', 'model_id', 'model_version', 'input_hash', 'seed', 'environment', 'started_at', 'result_ref'], 90), 'lab_runs.csv': (['experiment_id', 'sample_id', 'protocol_version', 'instrument_id', 'started_at', 'run_state', 'result_ref'], 75), 'measurements.csv': (['result_ref', 'metric', 'value', 'unit', 'uncertainty', 'replicate_no'], 120), 'validation_links.csv': (['link_id', 'simulation_id', 'experiment_id', 'condition_map_version', 'review_state', 'reviewer', 'decision_at'], 60)}

class MaterialTests(unittest.TestCase):
    def test_csv_contract(self):
        for filename,(columns,count) in EXPECTED.items():
            with self.subTest(filename=filename):
                with (ROOT/"data"/filename).open(encoding="utf-8-sig",newline="") as stream:
                    reader=csv.DictReader(stream); rows=list(reader)
                self.assertEqual(reader.fieldnames,columns)
                self.assertEqual(len(rows),count)
                self.assertTrue(all(None not in row for row in rows))

if __name__=="__main__": unittest.main()
