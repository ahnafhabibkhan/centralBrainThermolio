BEGIN;

-- The boolean authorization helper can inspect hidden ancestors without exposing their rows.
DROP POLICY IF EXISTS archive_path_owner_select ON central_brain.library_nodes;
CREATE POLICY archive_path_owner_select ON central_brain.library_nodes FOR SELECT TO central_brain_owner USING (
 workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid
);

CREATE OR REPLACE FUNCTION central_brain.archive_path_reviewable(proposal_workspace uuid, proposal_payload jsonb)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 WITH RECURSIVE
 requested_paths(parts) AS (
  SELECT string_to_array(lower(trim(both '/' from proposal_payload->>'folder_path')),'/')
  UNION
  SELECT string_to_array(lower(trim(both '/' from proposal_payload->>'folder_path')),'/')
         || (string_to_array(lower(f->>'name'),'/'))[1:cardinality(string_to_array(f->>'name','/'))-1]
  FROM jsonb_array_elements(coalesce(proposal_payload->'files','[]'::jsonb)) f
 ),
 prefixes(parts) AS (
  SELECT DISTINCT parts[1:depth] FROM requested_paths CROSS JOIN LATERAL generate_subscripts(parts,1) depth
 ),
 actual_paths(id,parent_id,kind,status,visibility,sensitivity,parts) AS (
  SELECT n.id,n.parent_id,n.kind,n.status,n.visibility,n.sensitivity,p.parts
  FROM prefixes p JOIN central_brain.library_nodes n
   ON cardinality(p.parts)=1 AND n.parent_id IS NULL AND lower(n.name)=p.parts[1]
  WHERE n.workspace_id=proposal_workspace AND n.status<>'rejected'
  UNION ALL
  SELECT n.id,n.parent_id,n.kind,n.status,n.visibility,n.sensitivity,p.parts
  FROM actual_paths previous JOIN prefixes p
   ON cardinality(p.parts)=cardinality(previous.parts)+1
   AND p.parts[1:cardinality(previous.parts)]=previous.parts
  JOIN central_brain.library_nodes n
   ON n.parent_id=previous.id AND lower(n.name)=p.parts[cardinality(p.parts)]
  WHERE n.workspace_id=proposal_workspace AND n.status<>'rejected'
 ),
 recorded_ids(id) AS (
  SELECT value::uuid FROM jsonb_array_elements_text(
   coalesce(proposal_payload->'destination_ancestors','[]'::jsonb))
 ),
 recorded_chains(id,parent_id,kind,status,visibility,sensitivity,seen) AS (
  SELECT n.id,n.parent_id,n.kind,n.status,n.visibility,n.sensitivity,ARRAY[n.id]
  FROM recorded_ids r JOIN central_brain.library_nodes n ON n.id=r.id
  WHERE n.workspace_id=proposal_workspace
  UNION ALL
  SELECT n.id,n.parent_id,n.kind,n.status,n.visibility,n.sensitivity,previous.seen||n.id
  FROM recorded_chains previous JOIN central_brain.library_nodes n ON n.id=previous.parent_id
  WHERE n.workspace_id=proposal_workspace AND cardinality(previous.seen)<32 AND NOT n.id=ANY(previous.seen)
 ),
 allowed_levels(value) AS (
  SELECT jsonb_array_elements_text(
   coalesce(nullif(current_setting('app.archive_review_sensitivities',true),''),'[]')::jsonb)
 )
 SELECT coalesce(
  proposal_workspace=nullif(current_setting('app.workspace_id',true),'')::uuid
  AND current_setting('app.can_review_archives',true)='true'
  AND jsonb_typeof(proposal_payload->'folder_path')='string'
  AND length(trim(both '/' from proposal_payload->>'folder_path'))>0
  AND NOT EXISTS (
   SELECT 1 FROM actual_paths
   WHERE kind<>'folder' OR status<>'active' OR visibility<>'workspace'
    OR sensitivity NOT IN (SELECT value FROM allowed_levels)
  )
  AND NOT EXISTS (SELECT 1 FROM actual_paths GROUP BY parts HAVING count(*)>1)
  AND NOT EXISTS (
   SELECT 1 FROM recorded_ids r
   WHERE NOT EXISTS (SELECT 1 FROM recorded_chains c WHERE c.id=r.id)
  )
  AND NOT EXISTS (
   SELECT 1 FROM recorded_chains
   WHERE kind<>'folder' OR status<>'active' OR visibility<>'workspace'
    OR sensitivity NOT IN (SELECT value FROM allowed_levels)
    OR (parent_id IS NOT NULL AND (cardinality(seen)=32 OR parent_id=ANY(seen)))
  ),false)
$$;
ALTER FUNCTION central_brain.archive_path_reviewable(uuid,jsonb) OWNER TO central_brain_owner;
REVOKE ALL ON FUNCTION central_brain.archive_path_reviewable(uuid,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION central_brain.archive_path_reviewable(uuid,jsonb) TO central_brain_runtime;

-- Shared archives require a workspace reviewer, even when another actor uploaded them.
-- Existing author policies continue to protect other organization suggestions and inserts.
DROP POLICY IF EXISTS archive_reviewer_select ON central_brain.library_suggestions;
CREATE POLICY archive_reviewer_select ON central_brain.library_suggestions FOR SELECT USING (
 action='archive' AND central_brain.archive_path_reviewable(workspace_id,payload)
);
DROP POLICY IF EXISTS archive_reviewer_update ON central_brain.library_suggestions;
CREATE POLICY archive_reviewer_update ON central_brain.library_suggestions FOR UPDATE USING (
 action='archive' AND central_brain.archive_path_reviewable(workspace_id,payload)
) WITH CHECK (
 action='archive' AND central_brain.archive_path_reviewable(workspace_id,payload)
);

-- Partial reviews reduce the reservation without changing the uploader's identity.
DROP POLICY IF EXISTS archive_review_update ON central_brain.transfer_reservations;
CREATE POLICY archive_review_update ON central_brain.transfer_reservations FOR UPDATE USING (
 workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid
 AND (
  created_by=nullif(current_setting('app.actor_id',true),'')::uuid
  OR (
   current_setting('app.can_review_archives',true)='true'
   AND EXISTS (
    SELECT 1 FROM central_brain.library_suggestions s
    WHERE s.id=transfer_reservations.id AND s.workspace_id=transfer_reservations.workspace_id
    AND s.created_by=transfer_reservations.created_by AND s.action='archive' AND s.status='proposed'
   )
  )
 )
) WITH CHECK (
 workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid
 AND (
  created_by=nullif(current_setting('app.actor_id',true),'')::uuid
  OR (
   current_setting('app.can_review_archives',true)='true'
   AND EXISTS (
    SELECT 1 FROM central_brain.library_suggestions s
    WHERE s.id=transfer_reservations.id AND s.workspace_id=transfer_reservations.workspace_id
    AND s.created_by=transfer_reservations.created_by AND s.action='archive' AND s.status='proposed'
   )
  )
 )
);
DROP POLICY IF EXISTS archive_reviewer_delete ON central_brain.transfer_reservations;
CREATE POLICY archive_reviewer_delete ON central_brain.transfer_reservations FOR DELETE USING (
 workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid
 AND current_setting('app.can_review_archives',true)='true'
 AND EXISTS (
  SELECT 1 FROM central_brain.library_suggestions s
  WHERE s.id=transfer_reservations.id AND s.workspace_id=transfer_reservations.workspace_id
  AND s.created_by=transfer_reservations.created_by AND s.action='archive' AND s.status='proposed'
 )
);
GRANT UPDATE(size_bytes) ON central_brain.transfer_reservations TO central_brain_runtime;

INSERT INTO central_brain.schema_migrations(version) VALUES('010_workspace_archive_review') ON CONFLICT DO NOTHING;
COMMIT;
