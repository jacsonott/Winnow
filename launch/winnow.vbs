' Double-click launcher for Windows with NO console window. Runs winnow.bat
' hidden; Winnow opens its own app window as usual.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
sh.Run "cmd /c """ & fso.BuildPath(here, "winnow.bat") & """", 0, False
