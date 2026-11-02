// Reference-only facade for SimHub.Logging.dll. See README.md in this folder.
//
// The real type is the static class `SimHub.Logging`, whose `Current` property
// returns log4net's ILog -- which is why the plugin build needs a log4net
// reference even though it never names log4net directly.

namespace SimHub
{
    public static class Logging
    {
        public static log4net.ILog Current
        {
            get { return null; }
        }

        public static void FlushBuffers()
        {
        }

        public static void AddDebugChartPoint(string series, double value)
        {
        }

        public static void AddDebugString(string key, string value)
        {
        }
    }
}
