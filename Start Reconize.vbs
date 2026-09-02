' Double-click this file to launch Reconize.
' It starts the backend and frontend servers completely hidden in the
' background (only if they aren't already running — safe to double-click
' more than once), then opens the app in Chrome automatically.

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

Function IsUp(url)
    On Error Resume Next
    Dim http
    Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
    http.SetTimeouts 1000, 1000, 1500, 1500
    http.Open "GET", url, False
    http.Send
    IsUp = (Err.Number = 0) And (http.Status >= 200) And (http.Status < 500)
    Err.Clear
    On Error Goto 0
End Function

backendWasUp = IsUp("http://127.0.0.1:8000/api/health")
If Not backendWasUp Then
    shell.CurrentDirectory = scriptDir & "\backend"
    shell.Run "cmd /c "".venv\Scripts\python.exe -m uvicorn app.main:app --port 8000""", 0, False
End If

frontendWasUp = IsUp("http://localhost:5173/")
If Not frontendWasUp Then
    shell.CurrentDirectory = scriptDir & "\frontend"
    shell.Run "cmd /c npm run dev", 0, False
End If

' Only wait if we actually had to start something new
If Not backendWasUp Or Not frontendWasUp Then
    WScript.Sleep 6000
End If

shell.Run "chrome http://localhost:5173", 1, False
