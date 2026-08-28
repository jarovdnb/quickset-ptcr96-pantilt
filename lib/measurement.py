#   *** THE ONE FILE MOST PEOPLE NEED TO EDIT ***
#
#   Everything else in this app -- moving the positioner, scheduling program
#   points, keep-out enforcement, the dashboard -- works out of the box for
#   any Quickset/PTCR-96-based pan-tilt unit. The only thing this app cannot
#   know for you is what "take a measurement" means for YOUR instrument.
#
#   pantilt.py calls take_measurement() once per scheduled point, with the
#   positioner already moved and settled at that point. Replace the body
#   below with whatever triggers your own instrument: call an API, run a
#   script, write a flag file/GPIO pin, grab a camera frame, etc.

def take_measurement(pan_deg: float, tilt_deg: float) -> str:
    """
    Called once per scheduled point, after the positioner has settled at
    (pan_deg, tilt_deg) (relative degrees, same convention as pantilt.py's
    live pan_rel/tilt_rel). This call BLOCKS the scheduler until it
    returns, so it's fine to wait here for your own measurement to
    actually finish -- do whatever synchronous or asynchronous work your
    instrument needs internally.

    Return one of:
      "done"        the measurement completed normally.
      "unreachable" your instrument could not be reached/triggered this
                     time; the scheduler treats this as expected and
                     recoverable (retries a few times, then skips to the
                     next point) rather than raising a positioner fault.

    The placeholder below does nothing but simulate success -- replace it.
    """
    print(f"take_measurement({pan_deg}, {tilt_deg}) called -- edit lib/measurement.py "
          f"to trigger your own instrument here.")
    return "done"
