// Reference-only facade for SimHub.Plugins.dll. See README.md in this folder.
//
// Only what ScApiPlugin.cs touches is declared. Signatures are copied from the
// real assembly by reflection, because an extension method call compiles to a
// direct static call naming its declaring type: `IPluginExtensions` must keep
// its exact name and namespace or the call would not resolve at runtime.

using System;

namespace SimHub.Plugins
{
    public class PluginManager
    {
    }

    public interface IPlugin
    {
        PluginManager PluginManager { get; set; }
        void Init(PluginManager pluginManager);
        void End(PluginManager pluginManager);
    }

    public interface IDataPlugin : IPlugin
    {
        void DataUpdate(PluginManager pluginManager, ref GameReaderCommon.GameData data);
    }

    public class EventTrigger
    {
        public void Trigger()
        {
        }
    }

    public enum SupportStatus
    {
        Supported = 0,
        NotSupported = 1,
        Computed = 2,
    }

    [AttributeUsage(AttributeTargets.Class, AllowMultiple = false)]
    public sealed class PluginNameAttribute : Attribute
    {
        public PluginNameAttribute(string name)
        {
        }
    }

    [AttributeUsage(AttributeTargets.Class, AllowMultiple = false)]
    public sealed class PluginAuthorAttribute : Attribute
    {
        public PluginAuthorAttribute(string name)
        {
        }
    }

    [AttributeUsage(AttributeTargets.Class, AllowMultiple = false)]
    public sealed class PluginDescriptionAttribute : Attribute
    {
        public PluginDescriptionAttribute(string description)
        {
        }
    }

    public static class IPluginExtensions
    {
        public static void AttachDelegate<T, U>(this T plugin, string name, Func<U> valueProvider)
            where T : IPlugin
        {
        }

        public static void AttachDelegate<T, U>(this T plugin, string name, Func<U> valueProvider, bool persistent)
            where T : IPlugin
        {
        }

        public static EventTrigger AddEvent<T>(this T plugin, string eventName)
            where T : IPlugin
        {
            return null;
        }

        public static void TriggerEvent<T>(this T plugin, string eventName)
            where T : IPlugin
        {
        }
    }
}
