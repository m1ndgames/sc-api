// Reference-only facade for GameReaderCommon.dll. See README.md in this folder.
//
// `IDataPlugin.DataUpdate` takes `ref GameData`, so the type has to exist to
// compile the plugin. The plugin does not read it: Star Citizen is not a
// SimHub-supported game, so GameData stays empty. The simple properties are
// declared anyway, so using one later does not mean editing this file.

namespace GameReaderCommon
{
    public class StatusDataBase
    {
    }

    public class GameData
    {
        public System.DateTime FrameTime { get; }
        public System.DateTime FrameTimeUTC { get; }
        public bool RunningGameProcessDetected { get; set; }
        public bool GameRunning { get; }
        public System.Guid SessionId { get; }
        public System.Guid LapId { get; }
        public System.DateTime SessionStartDate { get; }
        public string GamePath { get; set; }
        public bool GamePaused { get; }
        public bool GameInMenu { get; }
        public bool Spectating { get; }
        public string GameName { get; set; }
        public string GameReaderVariant { get; set; }
        public bool GameReplay { get; set; }

        public StatusDataBase OldData;
        public StatusDataBase NewData;
    }
}
