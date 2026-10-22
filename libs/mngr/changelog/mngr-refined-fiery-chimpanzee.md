The image Dockerfile's comments describe the keyframe build context as `current.tar.gz`, a
`.checkpoint` marker and `apps/minds/scripts/git-manifest.json`, which `scripts/make_tar_of_repo.sh`
now stages there so `mngr create -t modal` and `mngr create -t docker` on this repo build. Comments only;
no build steps changed.
