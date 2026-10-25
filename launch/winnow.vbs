' Double-click launcher for Windows with NO console window. Runs winnow.bat
' hidden; Winnow opens its own app window as usual.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
' The path is wrapped in TWO pairs of quotes on purpose. cmd /c keeps a
' single quoted path intact only when nothing in it is one of &<>()@^| —
' otherwise it strips the first and last quote and runs the bare path,
' so an install unzipped as "winnow-0.3.0 (1)" ran "C:\...\winnow-0.3.0"
' and nothing started. The outer pair is the one it strips.
sh.Run "cmd /c """"" & fso.BuildPath(here, "winnow.bat") & """""", 0, False
