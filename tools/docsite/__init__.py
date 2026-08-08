"""Static help-site generator for Project CHAOS.

Standard library only. Nothing here imports the ``chaos`` package, so the site
can be regenerated on a machine where the platform is not installed — which is
the point: the help has to exist when the platform does not.
"""

__all__ = ["build", "highlight", "mdparse", "theme"]
