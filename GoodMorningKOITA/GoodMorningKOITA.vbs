' GoodMorningKOITA: run without a CMD window (double-click this file).
' First run: shows the setup window (run.bat --setup) once to install libraries.
' Then starts the server hidden (pythonw launcher.py) and opens the program in Chrome (a normal tab).
' Closing all program tabs stops the program. For a CMD window (debugging) use run.bat.
Option Explicit
Dim sh, fso, dir, rc
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
If Not fso.FileExists(dir & "\.venv\Scripts\pythonw.exe") Then
  rc = sh.Run("cmd /c run.bat --setup", 1, True)
  If rc <> 0 Then WScript.Quit 1
End If
sh.Run """" & dir & "\.venv\Scripts\pythonw.exe"" launcher.py", 0, False
