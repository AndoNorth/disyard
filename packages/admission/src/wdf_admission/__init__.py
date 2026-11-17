"""Job admission, layer 1 scheduling: it releases a submitted job by claiming its row and starting
its workflow, within the caps of the tenancy profile, so queued jobs wait in the intake queues and
the job table and not in Temporal."""
