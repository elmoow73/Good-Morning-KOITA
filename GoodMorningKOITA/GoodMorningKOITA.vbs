' GoodMorningKOITA: run without a CMD window (double-click this file).
' First run (or when requirements.txt changed): shows the setup window (run.bat --setup) once to install libraries.
' Then starts the server hidden (pythonw launcher.py) with a tray icon (bottom-right hidden icons) and opens the program in Chrome.
' Closing the Chrome tabs does NOT stop the program. To stop: tray icon > right-click > Exit. For a CMD window (debugging) use run.bat.
' --background (Windows startup autorun): start in the tray only, without opening Chrome.
Option Explicit
Dim sh, fso, dir, rc, args, i
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

Function ReadText(path)
  ReadText = ""
  If fso.FileExists(path) Then
    If fso.GetFile(path).Size > 0 Then ReadText = fso.OpenTextFile(path, 1).ReadAll
  End If
End Function

If Not fso.FileExists(dir & "\.venv\Scripts\pythonw.exe") _
   Or ReadText(dir & "\requirements.txt") <> ReadText(dir & "\.venv\requirements.installed.txt") Then
  rc = sh.Run("cmd /c run.bat --setup", 1, True)
  If rc <> 0 Then WScript.Quit 1
End If
args = ""
For i = 0 To WScript.Arguments.Count - 1
  args = args & " " & WScript.Arguments(i)
Next
sh.Run """" & dir & "\.venv\Scripts\pythonw.exe"" launcher.py" & args, 0, False
