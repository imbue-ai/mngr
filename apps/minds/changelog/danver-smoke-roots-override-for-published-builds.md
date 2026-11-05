- The smoke suite launches the app with its roots override under both names,
  `STUDIO_ROOTS_OVERRIDE` and the older `MINDS_DATA_HOME`. A published build
  cut before the rename reads only the older name, so it now keeps its state
  in the run's data home too, where step 3b looks for the app's own mngr.
