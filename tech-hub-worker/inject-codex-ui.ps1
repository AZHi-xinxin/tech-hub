param(
    [Parameter(Mandatory = $true)]
    [string]$MessageFile
)

$ErrorActionPreference = 'Stop'
$message = [IO.File]::ReadAllText($MessageFile, [Text.UTF8Encoding]::new($false))
if ([string]::IsNullOrWhiteSpace($message)) { throw 'Message is empty' }

Add-Type -AssemblyName System.Windows.Forms
$source = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class CodexUiNative {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);
  [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);
  [DllImport("user32.dll", SetLastError=true)] public static extern uint SendInput(uint count, INPUT[] inputs, int size);
  public struct RECT { public int Left, Top, Right, Bottom; }
  [StructLayout(LayoutKind.Sequential)] public struct INPUT { public uint type; public INPUTUNION data; }
  // The native INPUT union is 32 bytes on 64-bit Windows.
  [StructLayout(LayoutKind.Explicit, Size=32)] public struct INPUTUNION { [FieldOffset(0)] public KEYBDINPUT keyboard; }
  [StructLayout(LayoutKind.Sequential)] public struct KEYBDINPUT {
    public ushort virtualKey; public ushort scanCode; public uint flags; public uint time; public UIntPtr extraInfo;
  }
  public static void TypeUnicode(string text) {
    var inputs = new List<INPUT>();
    foreach (char ch in text) {
      var down = new INPUT(); down.type = 1; down.data.keyboard.scanCode = ch; down.data.keyboard.flags = 0x0004;
      var up = down; up.data.keyboard.flags = 0x0004 | 0x0002;
      inputs.Add(down); inputs.Add(up);
    }
    if (inputs.Count > 0 && SendInput((uint)inputs.Count, inputs.ToArray(), Marshal.SizeOf(typeof(INPUT))) == 0) {
      throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    }
  }
  public static void PressVirtualKey(ushort key) {
    var down = new INPUT(); down.type = 1; down.data.keyboard.virtualKey = key;
    var up = down; up.data.keyboard.flags = 0x0002;
    var inputs = new INPUT[] { down, up };
    if (SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(INPUT))) == 0) {
      throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    }
  }
}
'@
Add-Type -TypeDefinition $source

$window = Get-Process Code | Where-Object { $_.MainWindowHandle -ne 0 } | Sort-Object StartTime | Select-Object -First 1
if (-not $window) { throw 'VS Code main window not found' }
$handle = $window.MainWindowHandle
[CodexUiNative]::ShowWindow($handle, 9) | Out-Null
[CodexUiNative]::SetForegroundWindow($handle) | Out-Null
Start-Sleep -Milliseconds 500

$rect = New-Object CodexUiNative+RECT
if (-not [CodexUiNative]::GetWindowRect($handle, [ref]$rect)) { throw 'Cannot read VS Code window rectangle' }
$width = $rect.Right - $rect.Left
$height = $rect.Bottom - $rect.Top
if ($width -lt 700 -or $height -lt 500) { throw 'VS Code window is too small for safe injection' }

$inputX = [int]($rect.Left + 0.73 * $width)
$inputY = [int]($rect.Top + 0.90 * $height)
[Windows.Forms.Cursor]::Position = New-Object Drawing.Point($inputX, $inputY)
[CodexUiNative]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
[CodexUiNative]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 300

$singleLineMessage = $message.Replace("`r", '').Replace("`n", ' | ')
[CodexUiNative]::TypeUnicode($singleLineMessage)
Start-Sleep -Milliseconds 700

# Codex submits on plain Enter; unlike a fixed button coordinate, this remains
# stable when a long message expands the composer.
[CodexUiNative]::PressVirtualKey(0x0D)
Start-Sleep -Milliseconds 700

Write-Output "Injected into VS Codex UI"
