// sc-api SimHub plugin -- consumes Star Citizen haptic telemetry.
//
// Star Citizen is NOT a SimHub-supported game: there is no SC game reader, so
// SimHub's normalized GameData stays empty and IDataPlugin.DataUpdate reports
// GameRunning == false. This plugin is therefore SELF-DRIVEN: it runs its own
// UDP listener and publishes what it receives as SimHub properties and events.
//
//   Star Citizen -> dbxLive64.dll -> sc-api server -> UDP JSON -> this plugin
//                                                                    |
//                                            properties + events -> ShakeIt
//
// Properties are published with AttachDelegate, so they are evaluated on demand
// and usable in ShakeIt / dashboard formulas. Discrete happenings (footstep,
// shot, impact, heartbeat) are additionally raised as SimHub events, which
// ShakeIt effects can trigger from directly.
//
// Wire format, one JSON object per datagram:
//   {"seq":123,"t":1760000000.1,"props":{"sc.vehicle.speed":180.2, ...}}
//
// Discrete events arrive as MONOTONIC COUNTERS (`*.count`) rather than one-shot
// flags, so a lost datagram cannot swallow an event -- we diff the counter and
// still know how many occurred.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Threading;

using Newtonsoft.Json.Linq;
using SimHub.Plugins;

namespace ScApi.Telemetry
{
    [PluginDescription("Star Citizen haptic telemetry via sc-api (D-BOX LiveMotion bridge)")]
    [PluginAuthor("sc-api")]
    [PluginName("sc-api Star Citizen Telemetry")]
    public class ScApiPlugin : IPlugin, IDataPlugin
    {
        public const int DefaultPort = 51680;

        // Properties we know the names of up front. Keep in sync with
        // scapi/mapping.py -- that file is the source of truth and records how
        // confident each identification is.
        private static readonly string[] KnownScalars =
        {
            "sc.vehicle.speed",
            "sc.vehicle.decoupled",
            "sc.actor.heart_rate",
            "sc.actor.heartbeat_bpm",
            "sc.weapon.shot_fired",
        };

        private static readonly string[] KnownVectors =
        {
            "sc.vehicle.orientation",
            "sc.vehicle.angular_velocity",
            "sc.vehicle.velocity",
            "sc.vehicle.accel",
            "sc.vehicle.accel_alt",
            "sc.vehicle.vec34",
            "sc.vehicle.vec35",
            "sc.vehicle.vec39",
            "sc.weapon.recoil",
            "sc.weapon.recoil_b",
            "sc.weapon.recoil_c",
            "sc.weapon.recoil_d",
            "sc.impact.vector",
            "sc.impact.a",
            "sc.impact.b",
            "sc.impact.c",
        };

        // counter property -> SimHub event name
        private static readonly Dictionary<string, string> CounterEvents =
            new Dictionary<string, string>
            {
                { "sc.actor.footstep.count",        "ScApiFootstep" },
                { "sc.actor.heartbeat.count",       "ScApiHeartbeat" },
                { "sc.impact.count",                "ScApiImpact" },
                { "sc.vehicle.transition.count",    "ScApiVehicleTransition" },
            };

        // Decaying pulse properties, in milliseconds of decay.
        //
        // ShakeIt custom effects are driven by FORMULAS over properties, not by
        // SimHub events, and discrete happenings reach us as monotonic counters
        // -- useless in a formula. These properties instead ramp 100 -> 0 over
        // a window after each occurrence, which is directly usable as effect
        // amplitude and gives a natural decay envelope.
        //
        // Computed on demand from "time since last occurrence", so there is no
        // timer thread and no cost when nothing is reading them.
        private static readonly Dictionary<string, double> PulseWindowsMs =
            new Dictionary<string, double>
            {
                { "sc.pulse.footstep",  120.0 },
                { "sc.pulse.shot",      110.0 },
                { "sc.pulse.impact",    260.0 },
                { "sc.pulse.heartbeat", 160.0 },
            };

        // which counter / level property drives which pulse
        private static readonly Dictionary<string, string> PulseSources =
            new Dictionary<string, string>
            {
                { "sc.actor.footstep.count",  "sc.pulse.footstep" },
                { "sc.actor.heartbeat.count", "sc.pulse.heartbeat" },
                { "sc.impact.count",          "sc.pulse.impact" },
            };

        public PluginManager PluginManager { get; set; }

        private readonly object _gate = new object();
        private readonly Dictionary<string, object> _values =
            new Dictionary<string, object>(StringComparer.Ordinal);
        private readonly Dictionary<string, double> _counters =
            new Dictionary<string, double>(StringComparer.Ordinal);
        private readonly Dictionary<string, int> _pending =
            new Dictionary<string, int>(StringComparer.Ordinal);
        private readonly HashSet<string> _attached =
            new HashSet<string>(StringComparer.Ordinal);
        private readonly Dictionary<string, DateTime> _pulseAt =
            new Dictionary<string, DateTime>(StringComparer.Ordinal);

        private UdpClient _udp;
        private Thread _thread;
        private volatile bool _stop;

        private long _datagrams;
        private long _seqGaps;
        private long _lastSeq = -1;
        private string _source = "";
        private DateTime _lastRx = DateTime.MinValue;
        private string _vehicleName = "";

        // Diagnostics: SimHub's web API does not expose arbitrary properties,
        // so without these the only way to tell whether telemetry is actually
        // arriving is to stare at the property list in the UI.
        private DateTime _lastStats = DateTime.MinValue;
        private static readonly TimeSpan StatsEvery = TimeSpan.FromSeconds(10);

        // ---------------------------------------------------------------- init

        public void Init(PluginManager pluginManager)
        {
            global::SimHub.Logging.Current.Info("sc-api: starting");

            foreach (var name in KnownScalars) Attach(name);
            foreach (var v in KnownVectors)
            {
                Attach(v + ".x");
                Attach(v + ".y");
                Attach(v + ".z");
            }
            foreach (var counter in CounterEvents.Keys) Attach(counter);

            // Pulses: amplitude 100 decaying linearly to 0 over the window.
            foreach (var kv in PulseWindowsMs)
            {
                var name = kv.Key;
                var windowMs = kv.Value;
                this.AttachDelegate(name, () => PulseValue(name, windowMs));
            }

            // Link health -- so a dashboard can show whether sc-api is alive
            // without having to interpret telemetry.
            this.AttachDelegate("sc.link.connected",
                () => (DateTime.UtcNow - LastRx).TotalSeconds < 2.0);
            this.AttachDelegate("sc.link.age_ms",
                () => LastRx == DateTime.MinValue
                        ? -1.0
                        : (DateTime.UtcNow - LastRx).TotalMilliseconds);
            this.AttachDelegate("sc.link.datagrams", () => (double)_datagrams);
            this.AttachDelegate("sc.link.seq_gaps", () => (double)_seqGaps);
            this.AttachDelegate("sc.vehicle.name", () => VehicleName);

            foreach (var ev in CounterEvents.Values) this.AddEvent(ev);
            this.AddEvent("ScApiShotFired");

            StartListener(DefaultPort);
        }

        private DateTime LastRx
        {
            get { lock (_gate) { return _lastRx; } }
        }

        /// 100 immediately after the event, decaying linearly to 0 over
        /// windowMs, then flat 0. Safe to call at any rate.
        private double PulseValue(string name, double windowMs)
        {
            DateTime at;
            lock (_gate)
            {
                if (!_pulseAt.TryGetValue(name, out at)) return 0.0;
            }
            var elapsed = (DateTime.UtcNow - at).TotalMilliseconds;
            if (elapsed < 0.0 || elapsed >= windowMs) return 0.0;
            return 100.0 * (1.0 - (elapsed / windowMs));
        }

        private void MarkPulse(string name)
        {
            // Caller holds _gate.
            _pulseAt[name] = DateTime.UtcNow;
        }

        private string VehicleName
        {
            get { lock (_gate) { return _vehicleName; } }
        }

        /// Publish one property, reading from the shared snapshot on demand.
        private void Attach(string name)
        {
            if (!_attached.Add(name)) return;
            var key = name;
            this.AttachDelegate(key, () =>
            {
                lock (_gate)
                {
                    object v;
                    return _values.TryGetValue(key, out v) ? v : (object)0.0;
                }
            });
        }

        // ------------------------------------------------------------ listener

        private void StartListener(int port)
        {
            try
            {
                _udp = new UdpClient(new IPEndPoint(IPAddress.Loopback, port));
                // Do not let a slow read block shutdown.
                _udp.Client.ReceiveTimeout = 500;
            }
            catch (Exception e)
            {
                global::SimHub.Logging.Current.Error("sc-api: cannot bind UDP " + port + ": " + e.Message);
                return;
            }

            _thread = new Thread(ReceiveLoop)
            {
                IsBackground = true,
                Name = "sc-api-udp",
            };
            _thread.Start();
            global::SimHub.Logging.Current.Info("sc-api: listening on udp://127.0.0.1:" + port);
        }

        private void ReceiveLoop()
        {
            var any = new IPEndPoint(IPAddress.Any, 0);
            while (!_stop)
            {
                byte[] data;
                try
                {
                    data = _udp.Receive(ref any);
                }
                catch (SocketException)
                {
                    continue;   // timeout, or transient
                }
                catch (ObjectDisposedException)
                {
                    return;     // shutting down
                }

                try
                {
                    Ingest(System.Text.Encoding.UTF8.GetString(data));
                }
                catch (Exception e)
                {
                    // Never let a malformed datagram kill the listener.
                    global::SimHub.Logging.Current.Debug("sc-api: bad datagram: " + e.Message);
                }
            }
        }

        private void Ingest(string json)
        {
            var msg = JObject.Parse(json);
            var props = msg["props"] as JObject;
            if (props == null) return;

            var seq = msg["seq"] != null ? msg["seq"].Value<long>() : -1;
            var src = msg["src"] != null ? msg["src"].Value<string>() : "";

            bool logFirst = false;
            bool logStats = false;
            bool logReset = false;
            long dg = 0, gaps = 0;
            int distinct = 0;

            lock (_gate)
            {
                _datagrams++;
                if (_datagrams == 1) logFirst = true;

                // A different publisher instance, or a sequence that went
                // backwards, means the counters restarted from zero. Treating
                // that as "events happened" would fire a burst of spurious
                // haptics -- and with two publishers alive at once (say two
                // replays) the counters interleave and retrigger constantly.
                // So: adopt the new source, drop the old counters, and emit no
                // events for this datagram.
                var newSource = (src != _source) || (seq >= 0 && seq < _lastSeq);
                if (newSource)
                {
                    if (_source.Length > 0) logReset = true;
                    _source = src;
                    _counters.Clear();
                    _pending.Clear();
                    _lastSeq = -1;
                }

                if (DateTime.UtcNow - _lastStats > StatsEvery)
                {
                    _lastStats = DateTime.UtcNow;
                    logStats = _datagrams > 1;
                }
                _lastRx = DateTime.UtcNow;
                if (!newSource && _lastSeq >= 0 && seq > _lastSeq + 1)
                    _seqGaps += seq - _lastSeq - 1;
                if (seq >= 0) _lastSeq = seq;

                foreach (var kv in props)
                {
                    var name = kv.Key;
                    var tok = kv.Value;

                    if (tok.Type == JTokenType.String)
                    {
                        var s = tok.Value<string>();
                        if (name == "sc.vehicle.name") _vehicleName = s;
                        _values[name] = s;
                        continue;
                    }

                    double d;
                    try { d = tok.Value<double>(); }
                    catch (Exception) { continue; }

                    _values[name] = d;

                    // Counters: diff against the last value to recover how many
                    // discrete events happened, even across a lost datagram.
                    if (name.EndsWith(".count", StringComparison.Ordinal))
                    {
                        double prev;
                        if (_counters.TryGetValue(name, out prev))
                        {
                            var delta = (int)Math.Round(d - prev);
                            // Guard against a counter jump that cannot be real.
                            if (delta > 32) delta = 0;
                            if (delta > 0)
                            {
                                string pulse;
                                if (PulseSources.TryGetValue(name, out pulse))
                                    MarkPulse(pulse);

                                string ev;
                                if (CounterEvents.TryGetValue(name, out ev))
                                {
                                    int cur;
                                    _pending.TryGetValue(ev, out cur);
                                    // Cap: after a long stall, do not fire
                                    // hundreds of haptic events at once.
                                    _pending[ev] = Math.Min(cur + delta, 4);
                                }
                            }
                        }
                        _counters[name] = d;
                    }
                    else if (name == "sc.weapon.shot_fired" && d > 0.0)
                    {
                        // Spikes for a single game frame, which a formula
                        // sampled at a different rate can easily miss -- so
                        // stretch it into a decaying pulse as well.
                        MarkPulse("sc.pulse.shot");

                        int cur;
                        _pending.TryGetValue("ScApiShotFired", out cur);
                        _pending["ScApiShotFired"] = Math.Min(cur + 1, 4);
                    }
                }

                dg = _datagrams;
                gaps = _seqGaps;
                distinct = _values.Count;
            }

            // Logged outside the lock: never hold it across I/O.
            if (logReset)
            {
                global::SimHub.Logging.Current.Info(
                    "sc-api: publisher changed or restarted (src=" + src +
                    ") -- counters reset, events suppressed for this frame");
            }

            if (logFirst)
            {
                global::SimHub.Logging.Current.Info(string.Format(
                    CultureInfo.InvariantCulture,
                    "sc-api: first telemetry received -- {0} properties, vehicle '{1}'",
                    distinct, _vehicleName));
            }
            else if (logStats)
            {
                global::SimHub.Logging.Current.Info(string.Format(
                    CultureInfo.InvariantCulture,
                    "sc-api: {0} datagrams, {1} seq gaps, {2} properties live",
                    dg, gaps, distinct));
            }
        }

        // ------------------------------------------------------------- SimHub

        /// Called once per SimHub data tick. Kept deliberately cheap: it only
        /// drains queued events, because this method is on SimHub's critical
        /// path. Events are raised here rather than from the UDP thread so they
        /// are delivered on the thread SimHub expects.
        public void DataUpdate(PluginManager pluginManager, ref GameReaderCommon.GameData data)
        {
            List<string> fire = null;
            lock (_gate)
            {
                if (_pending.Count > 0)
                {
                    fire = new List<string>();
                    foreach (var kv in _pending)
                        for (var i = 0; i < kv.Value; i++) fire.Add(kv.Key);
                    _pending.Clear();
                }
            }
            if (fire == null) return;

            foreach (var ev in fire)
            {
                try { this.TriggerEvent(ev); }
                catch (Exception e)
                {
                    global::SimHub.Logging.Current.Debug("sc-api: TriggerEvent " + ev + ": " + e.Message);
                }
            }
        }

        public void End(PluginManager pluginManager)
        {
            global::SimHub.Logging.Current.Info(string.Format(
                CultureInfo.InvariantCulture,
                "sc-api: stopping ({0} datagrams, {1} seq gaps)",
                _datagrams, _seqGaps));

            _stop = true;
            try { if (_udp != null) _udp.Close(); } catch (Exception) { }
            try { if (_thread != null) _thread.Join(1000); } catch (Exception) { }
        }
    }
}
