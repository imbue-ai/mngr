A workspace that is still preparing an update no longer flips to "Updating..." whenever it briefly stops answering.
The app's recovery guard used to mark the row as applying for six minutes whenever it could not read the workspace's update record, which on a heavily loaded workspace happened on every short outage.
It now still holds off restarting that workspace but leaves the row showing where the run actually is (still preparing, or waiting on the user), and it only reports an apply when the workspace says one is under way.
The guard also no longer holds off (or later restarts) a workspace that answered again while the guard was still asking it about its update.
