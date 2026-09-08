class WMACConstants:
  # Lead detection parameters
  LEAD_WINDOW_SIZE = 6  # Stable detection window
  LEAD_PROB = 0.45  # Balanced threshold for lead detection

  # Slow down detection parameters
  SLOW_DOWN_WINDOW_SIZE = 5  # Responsive but stable
  SLOW_DOWN_PROB = 0.3  # Balanced threshold for slow down scenarios

  # Optimized slow down distance curve - smooth and progressive.
  # Above 60 km/h the curve continues at 2.5 m per km/h, tracking roughly 95% of the distance
  # covered in the model's 10 s horizon. It stops at 150 km/h: V_CRUISE_MAX is 145, and interp
  # clamps past the last breakpoint, which is the conservative direction.
  SLOW_DOWN_BP = [0., 10., 20., 30., 40., 50., 55., 60., 70., 80., 90., 100., 110., 120., 130., 140., 150.]
  SLOW_DOWN_DIST = [32., 46., 64., 86., 108., 130., 145., 165., 190., 215., 240., 265., 290., 315., 340., 365., 390.]

  # Slowness detection parameters
  SLOWNESS_WINDOW_SIZE = 10  # Stable slowness detection
  SLOWNESS_PROB = 0.55  # Clear threshold for slowness
  SLOWNESS_CRUISE_OFFSET = 1.025  # Conservative cruise speed offset
