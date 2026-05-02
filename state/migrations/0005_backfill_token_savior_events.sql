INSERT INTO token_savior_events (
    ts, ts_epoch, task_id, feature_id, project, role, status,
    query_present, sections_json, rows_found, context_chars,
    estimated_context_tokens, estimated_full_read_chars,
    estimated_tokens_saved, native_total_calls, native_tokens_used,
    native_tokens_naive, native_tokens_saved, payload_json
)
SELECT
    json_extract(e.payload_json, '$.ts') AS ts,
    e.created_at_epoch AS ts_epoch,
    json_extract(e.payload_json, '$.task_id') AS task_id,
    json_extract(e.payload_json, '$.feature_id') AS feature_id,
    json_extract(e.payload_json, '$.details.project') AS project,
    json_extract(e.payload_json, '$.details.role') AS role,
    COALESCE(json_extract(e.payload_json, '$.details.status'), 'unknown') AS status,
    CASE WHEN json_extract(e.payload_json, '$.details.query_present') THEN 1 ELSE 0 END AS query_present,
    COALESCE(json_extract(e.payload_json, '$.details.sections'), '[]') AS sections_json,
    COALESCE(json_extract(e.payload_json, '$.details.rows_found'), 0) AS rows_found,
    COALESCE(json_extract(e.payload_json, '$.details.context_chars'), 0) AS context_chars,
    COALESCE(json_extract(e.payload_json, '$.details.estimated_context_tokens'), 0) AS estimated_context_tokens,
    COALESCE(json_extract(e.payload_json, '$.details.estimated_full_read_chars'), 0) AS estimated_full_read_chars,
    COALESCE(json_extract(e.payload_json, '$.details.estimated_tokens_saved'), 0) AS estimated_tokens_saved,
    COALESCE(json_extract(e.payload_json, '$.details.native_stats.total_calls'), 0) AS native_total_calls,
    COALESCE(json_extract(e.payload_json, '$.details.native_stats.total_tokens_used'), 0) AS native_tokens_used,
    COALESCE(json_extract(e.payload_json, '$.details.native_stats.total_tokens_naive'), 0) AS native_tokens_naive,
    COALESCE(json_extract(e.payload_json, '$.details.native_stats.total_tokens_saved'), 0) AS native_tokens_saved,
    e.payload_json
FROM events AS e
WHERE e.kind = 'skills:token_savior_checked'
  AND NOT EXISTS (
      SELECT 1
        FROM token_savior_events AS tse
       WHERE tse.ts = json_extract(e.payload_json, '$.ts')
         AND COALESCE(tse.task_id, '') = COALESCE(json_extract(e.payload_json, '$.task_id'), '')
  );
