Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder
pythonw = fso.BuildPath(folder, ".venv\Scripts\pythonw.exe")
If Not fso.FileExists(pythonw) Then
    pythonw = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\pythonw.exe"
End If
If Not fso.FileExists(pythonw) Then pythonw = "pythonw.exe"
app = fso.BuildPath(folder, "app.py")
On Error Resume Next
shell.Run Chr(34) & pythonw & Chr(34) & " " & Chr(34) & app & Chr(34), 0, False
If Err.Number <> 0 Then MsgBox "Python not found. Use VoiceToAI.exe or install Python and requirements.txt.", 48, "Voice to AI"
