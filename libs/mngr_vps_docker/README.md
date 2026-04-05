# mngr VPS Docker Provider

Base classes and shared infrastructure for running mngr agents in Docker containers on VPS instances.

This package is a library -- it provides abstract base classes that concrete VPS provider implementations (like `mngr_vultr`) build on. It does not register any provider backends itself.
