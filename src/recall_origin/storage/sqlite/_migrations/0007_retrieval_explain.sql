ALTER TABLE retrieval_runs
ADD COLUMN query_shape_json TEXT NOT NULL DEFAULT '{}';

ALTER TABLE retrieval_runs
ADD COLUMN ranking_json TEXT NOT NULL DEFAULT '[]';

ALTER TABLE retrieval_runs
ADD COLUMN degradation_reasons_json TEXT NOT NULL DEFAULT '[]';
