# The daily run uses local launchd, not the cloud

The automatic daily run (for example at 8:00) runs on the user's own Mac with launchd, plus `pmset` to wake the Mac a few minutes earlier. It does not use GitHub Actions or a cloud scheduler. The reason is that `jobs.db`, `profiles/` and `resumes/` are personal data; moving them to the cloud would mean handling uploads, syncing the notes back to the user's notes folder, and storing the API keys. Keeping everything local leaves the user's existing note workflow unchanged.

Cost: the Mac has to be plugged in before sleep (a closed lid is fine) for the wake-up to be reliable. If a laptop is unplugged or never wakes during the day, that day's run does not happen; launchd runs it at the next wake and the make-up run is notified like any other. If this limit becomes unacceptable, the alternative is to move the data and the run to the cloud or an always-on host, which would require redoing the data sync.
