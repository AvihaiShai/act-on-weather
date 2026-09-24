-- A saved trip can be removed by its owner on this single-user workstation.
-- The consumer remains the only service with write grants. Remove revision
-- history too, since prior titles and plan days are user data.
GRANT DELETE ON itineraries, record_history TO aow_writer;
