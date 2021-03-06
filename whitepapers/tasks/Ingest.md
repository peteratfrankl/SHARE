# Ingest Task

## Responsibilities
* Load the most recent raw data for the given SUID.
* Transform the raw data into a MutableGraph.
* Apply source-specific and global regulations on the MutableGraph.
* Validate the regulated MutableGraph against a standard set of criteria and the SHARE data model.
* Update the SUID's existing set of States to match the validated MutableGraph.
* Keep the IngestLog for the given SUID accurate and up to date.
  * Catch any errors and add them to the IngestLog.
  * Store serialized snapshots of the MutableGraph after the Transform and Regulate steps in the IngestLog.


## Parameters
* `ingest_log_id` -- ID of the IngestLog for this task
* `superfluous` --


## Steps

### Setup
* Load the IngestLog
  * If not found, panic.
  * If `IngestLog.status` is `succeeded` and `superfluous` is `False`, exit.
* Obtain an Ingest lock on `suid_id`
  * If the lock times out/isn't granted, set `IngestLog.status` to `rescheduled` and raise a `Retry`.
* Load the most recent RawDatum for the given SUID.
  * If not found, log an error and exit.
  * TODO: Once `RawDatum.partial` is implemented, load all raw data from the most recent back to the last with `partial=False`.
  * If the SUID's latest RawDatum is more recent than `IngestLog.latest_raw_id`, update `IngestLog.latest_raw_id`
    * If update violates unique constraint, exit. Another task has already ingested the latest data.
  * Link the RawDatum to the `IngestLog`
* Set `IngestLog.status` to `started` and update `IngestLog.date_started`


### Ingest
* [Transform](../ingest/Transformer.md)
  * Load the Transformer from the SUID's SourceConfig.
  * Update `IngestLog.transformer_version`.
  * Use the Transformer to transform the raw data into a [MutableGraph](../ingest/Graph.md).
  * Serialize the MutableGraph to `IngestLog.transformed_data`.
* [Regulate](../ingest/Regulator.md)
  * Load the Regulator.
  * Update `IngestLog.regulator_version`.
  * Use the Regulator to clean the MutableGraph.
    * Save list of modifications with reasons to `IngestLog.regulator_log`.
  * Serialize the cleaned MutableGraph to `IngestLog.regulated_data`.
  * Use the Regulator to validate the cleaned MutableGraph.
* NOT IMPLEMENTED: [Consolidate](../ingest/Consolidator.md)
  * Load the Consolidator.
  * Update `IngestLog.consolidator_version`.
  * Use Consolidator to update the given SUID's States to match the validated MutableGraph.
* Until Consolidator is implemented:
  * Serialize MutableGraph to JSON-LD and save as NormalizedData.
  * Spawn DisambiguatorTask for the created NormalizedData.


### Cleanup
* Release all locks.
* Set `IngestLog.status` to `succeeded` and increment `IngestLog.completions`.


## Errors
If any errors arise while ingesting:
* Set `IngestLog.status` to `failed`.
* Set `IngestLog.context` to the traceback of the caught exception, or any other error information.
* Raise a `Retry`
