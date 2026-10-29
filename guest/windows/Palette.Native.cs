// Copyright Palette contributors. SPDX-License-Identifier: Apache-2.0
// Windows inbox .NET Framework / C# 5; no third-party guest runtime.
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace PaletteGuest {
internal static class Native {
    [StructLayout(LayoutKind.Sequential)] internal struct Rect { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] struct StartupInfo {
        public int cb; public string reserved, desktop, title; public int x,y,cx,cy,xc,yc,fill,flags;
        public short show,reserved2; public IntPtr reservedPtr,input,output,error;
    }
    [StructLayout(LayoutKind.Sequential)] struct ProcessInfo { public IntPtr process, thread; public uint pid, tid; }
    [StructLayout(LayoutKind.Sequential)] struct KeyboardInput { public ushort vk, scan; public uint flags,time; public UIntPtr extra; }
    [StructLayout(LayoutKind.Explicit, Size=32)] struct InputUnion { [FieldOffset(0)] public KeyboardInput keyboard; }
    [StructLayout(LayoutKind.Sequential)] struct Input { public uint type; public InputUnion data; }
    [StructLayout(LayoutKind.Sequential)] struct BasicJob { public long userTime,jobTime; public uint flags; public UIntPtr min,max; public uint active; public UIntPtr affinity; public uint priority,scheduling; }
    [StructLayout(LayoutKind.Sequential)] struct IoCounters { public ulong r,w,o,rb,wb,ob; }
    [StructLayout(LayoutKind.Sequential)] struct ExtendedJob { public BasicJob basic; public IoCounters io; public UIntPtr processMemory,jobMemory,peakProcess,peakJob; }
    internal delegate bool EnumWindowsProc(IntPtr window, IntPtr state);
    [DllImport("kernel32.dll", SetLastError=true)] internal static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll")] internal static extern uint WTSGetActiveConsoleSessionId();
    [DllImport("wtsapi32.dll", SetLastError=true)] static extern bool WTSQueryUserToken(uint session,out IntPtr token);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool GetTokenInformation(IntPtr token,int kind,IntPtr value,int length,out int returned);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool DuplicateTokenEx(IntPtr token,uint access,IntPtr attributes,int level,int type,out IntPtr copy);
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode,SetLastError=true)] static extern bool CreateProcessAsUser(IntPtr token,string app,StringBuilder args,IntPtr pa,IntPtr ta,bool inherit,uint flags,IntPtr env,string cwd,ref StartupInfo start,out ProcessInfo process);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode,SetLastError=true)] static extern bool CreateProcess(string app,StringBuilder args,IntPtr pa,IntPtr ta,bool inherit,uint flags,IntPtr env,string cwd,ref StartupInfo start,out ProcessInfo process);
    [DllImport("userenv.dll",SetLastError=true)] static extern bool CreateEnvironmentBlock(out IntPtr env,IntPtr token,bool inherit);
    [DllImport("userenv.dll")] static extern bool DestroyEnvironmentBlock(IntPtr env);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern SafeFileHandle CreateFile(string path,uint access,uint share,IntPtr sa,uint disposition,uint flags,IntPtr template);
    [DllImport("kernel32.dll",SetLastError=true)] internal static extern bool GetNamedPipeClientProcessId(SafePipeHandle pipe,out uint pid);
    [DllImport("user32.dll",SetLastError=true)] static extern IntPtr OpenInputDesktop(uint flags,bool inherit,uint access);
    [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool GetUserObjectInformation(IntPtr obj,int index,StringBuilder text,int length,out int needed);
    [DllImport("user32.dll")] static extern bool CloseDesktop(IntPtr desktop);
    [DllImport("user32.dll")] internal static extern bool EnumWindows(EnumWindowsProc callback,IntPtr state);
    [DllImport("user32.dll")] internal static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")] internal static extern bool IsWindow(IntPtr window);
    [DllImport("user32.dll")] internal static extern bool IsIconic(IntPtr window);
    [DllImport("user32.dll")] internal static extern bool IsZoomed(IntPtr window);
    [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr window,StringBuilder text,int count);
    [DllImport("user32.dll")] internal static extern uint GetWindowThreadProcessId(IntPtr window,out uint pid);
    [DllImport("user32.dll")] internal static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] internal static extern bool GetWindowRect(IntPtr window,out Rect rect);
    [DllImport("user32.dll")] internal static extern bool ShowWindow(IntPtr window,int command);
    [DllImport("user32.dll")] internal static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
    [DllImport("user32.dll",SetLastError=true)] static extern bool AttachThreadInput(uint first,uint second,bool attach);
    [DllImport("user32.dll")] static extern bool BringWindowToTop(IntPtr window);
    [DllImport("user32.dll",SetLastError=true)] internal static extern bool SetWindowPos(IntPtr window,IntPtr after,int x,int y,int width,int height,uint flags);
    [DllImport("user32.dll")] internal static extern int GetSystemMetrics(int metric);
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    [DllImport("user32.dll",SetLastError=true)] static extern uint SendInput(uint count,Input[] input,int size);
    [DllImport("user32.dll",SetLastError=true)] static extern bool OpenClipboard(IntPtr owner);
    [DllImport("user32.dll")] static extern bool CloseClipboard();
    [DllImport("user32.dll")] static extern bool EmptyClipboard();
    [DllImport("user32.dll")] static extern bool IsClipboardFormatAvailable(uint format);
    [DllImport("user32.dll")] static extern IntPtr GetClipboardData(uint format);
    [DllImport("user32.dll",SetLastError=true)] static extern IntPtr SetClipboardData(uint format,IntPtr memory);
    [DllImport("kernel32.dll",SetLastError=true)] static extern IntPtr GlobalAlloc(uint flags,UIntPtr bytes);
    [DllImport("kernel32.dll")] static extern IntPtr GlobalLock(IntPtr memory);
    [DllImport("kernel32.dll")] static extern bool GlobalUnlock(IntPtr memory);
    [DllImport("kernel32.dll")] static extern IntPtr GlobalFree(IntPtr memory);
    [DllImport("kernel32.dll")] static extern UIntPtr GlobalSize(IntPtr memory);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr sa,string name);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job,int kind,ref ExtendedJob info,uint size);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
    internal static void DpiAware() { SetProcessDPIAware(); }
    internal static bool FocusWindow(IntPtr window) {
        uint unused; uint current=GetCurrentThreadId();
        uint foreground=GetWindowThreadProcessId(GetForegroundWindow(),out unused);
        uint target=GetWindowThreadProcessId(window,out unused);
        bool joinedForeground=false,joinedTarget=false;
        try {
            if(foreground!=0&&foreground!=current) joinedForeground=AttachThreadInput(current,foreground,true);
            if(target!=0&&target!=current&&target!=foreground) joinedTarget=AttachThreadInput(current,target,true);
            if(IsIconic(window)) ShowWindow(window,9);
            BringWindowToTop(window); SetForegroundWindow(window);
            System.Threading.Thread.Sleep(100);
            return GetForegroundWindow()==window;
        } finally {
            if(joinedTarget) AttachThreadInput(current,target,false);
            if(joinedForeground) AttachThreadInput(current,foreground,false);
        }
    }
    internal static string Title(IntPtr hwnd) { var text=new StringBuilder(1024); GetWindowText(hwnd,text,text.Capacity); return text.ToString(); }
    internal static bool Interactive() {
        IntPtr desktop=OpenInputDesktop(0,false,1);
        if(desktop==IntPtr.Zero) return false;
        try { var name=new StringBuilder(128); int needed; return GetUserObjectInformation(desktop,2,name,256,out needed)&&name.ToString()=="Default"; }
        finally { CloseDesktop(desktop); }
    }
    internal static FileStream OpenPort() {
        var handle=CreateFile(@"\\.\Global\org.palette.tools.0",0xC0000000,3,IntPtr.Zero,3,0x40000000,IntPtr.Zero);
        if(handle.IsInvalid) { handle.Dispose(); throw new Win32Exception(); }
        return new FileStream(handle,FileAccess.ReadWrite,4096,true);
    }
    internal static string Quote(string value) {
        var b=new StringBuilder("\""); int slashes=0;
        foreach(char c in value) { if(c=='\\') { slashes++; continue; } if(c=='"') { b.Append('\\',slashes*2+1); b.Append(c); } else { b.Append('\\',slashes); b.Append(c); } slashes=0; }
        b.Append('\\',slashes*2); return b.Append('"').ToString();
    }
    internal static Process StartUserWorker(uint session,string executable,string pipe,out SecurityIdentifier sid) {
        IntPtr token=IntPtr.Zero, linked=IntPtr.Zero, primary=IntPtr.Zero, env=IntPtr.Zero, buffer=Marshal.AllocHGlobal(IntPtr.Size);
        try {
            if(!WTSQueryUserToken(session,out token)) throw new Win32Exception();
            int returned;
            // An administrator's active token may be elevated. Always choose the linked limited token.
            if(GetTokenInformation(token,18,buffer,IntPtr.Size,out returned)) {
                int type=Marshal.ReadInt32(buffer);
                if(type==2) { if(!GetTokenInformation(token,19,buffer,IntPtr.Size,out returned)) throw new Win32Exception(); linked=Marshal.ReadIntPtr(buffer); }
            }
            if(!DuplicateTokenEx(linked!=IntPtr.Zero?linked:token,0x02000000,IntPtr.Zero,2,1,out primary)) throw new Win32Exception();
            using(var identity=new WindowsIdentity(primary)) sid=identity.User;
            if(!CreateEnvironmentBlock(out env,primary,false)) throw new Win32Exception();
            // Let Windows select the logged-in user's session desktop. Explicitly naming
            // WinSta0\Default can make CreateProcessAsUser's child fail during DLL startup.
            // Interactive operations still require the worker's input-desktop check.
            var start=new StartupInfo(); start.cb=Marshal.SizeOf(start);
            ProcessInfo process;
            var args=new StringBuilder(Quote(executable)+" --worker "+Quote(pipe));
            if(!CreateProcessAsUser(primary,executable,args,IntPtr.Zero,IntPtr.Zero,false,0x08000400,env,Path.GetDirectoryName(executable),ref start,out process)) throw new Win32Exception();
            CloseHandle(process.thread); CloseHandle(process.process);
            return Process.GetProcessById((int)process.pid);
        } finally { Marshal.FreeHGlobal(buffer); if(env!=IntPtr.Zero) DestroyEnvironmentBlock(env); foreach(var h in new[]{primary,linked,token}) if(h!=IntPtr.Zero) CloseHandle(h); }
    }
    internal static SecurityIdentifier SessionUser(uint session) {
        IntPtr token; if(!WTSQueryUserToken(session,out token)) throw new Win32Exception();
        try { using(var identity=new WindowsIdentity(token)) return identity.User; } finally { CloseHandle(token); }
    }
    internal static uint StartDetachedApplication(string executable,string arguments,string cwd) {
        // Apps intentionally outlive a helper reconnect and inherit the worker's desktop.
        var start=new StartupInfo(); start.cb=Marshal.SizeOf(start);
        ProcessInfo process;
        if(!CreateProcess(executable,new StringBuilder(Quote(executable)+" "+arguments),IntPtr.Zero,IntPtr.Zero,false,0x01000000,IntPtr.Zero,cwd,ref start,out process)) throw new Win32Exception();
        CloseHandle(process.thread); CloseHandle(process.process); return process.pid;
    }
    internal static void TypeUnicode(string text) {
        var input=new Input[text.Length*2];
        for(int i=0;i<text.Length;i++) { input[2*i].type=1; input[2*i].data.keyboard.scan=text[i]; input[2*i].data.keyboard.flags=4; input[2*i+1]=input[2*i]; input[2*i+1].data.keyboard.flags=6; }
        if(SendInput((uint)input.Length,input,Marshal.SizeOf(typeof(Input)))!=input.Length) throw new InvalidOperationException("Unicode input was only partially submitted");
    }
    internal static string Clipboard(string text,bool write) {
        bool opened=false; for(int i=0;i<10&&!opened;i++) { opened=OpenClipboard(IntPtr.Zero); if(!opened) System.Threading.Thread.Sleep(20); }
        if(!opened) throw new InvalidOperationException("Clipboard is busy");
        try {
            if(!write) {
                if(!IsClipboardFormatAvailable(13)) return "";
                IntPtr mem=GetClipboardData(13); if(mem==IntPtr.Zero) return "";
                ulong bytes=GlobalSize(mem).ToUInt64(); if(bytes>196610) throw new InvalidOperationException("Clipboard exceeds the text limit");
                IntPtr ptr=GlobalLock(mem); if(ptr==IntPtr.Zero) throw new Win32Exception();
                try { string result=Marshal.PtrToStringUni(ptr,(int)bytes/2).Split('\0')[0]; if(Encoding.UTF8.GetByteCount(result)>49152) throw new InvalidOperationException("Clipboard exceeds 48 KiB"); return result; }
                finally { GlobalUnlock(mem); }
            }
            byte[] data=Encoding.Unicode.GetBytes(text+"\0"); IntPtr memory=GlobalAlloc(0x42,(UIntPtr)data.Length);
            if(memory==IntPtr.Zero) throw new Win32Exception();
            try { IntPtr ptr=GlobalLock(memory); if(ptr==IntPtr.Zero) throw new Win32Exception(); try { Marshal.Copy(data,0,ptr,data.Length); } finally { GlobalUnlock(memory); }
                if(!EmptyClipboard()||SetClipboardData(13,memory)==IntPtr.Zero) throw new Win32Exception(); memory=IntPtr.Zero;
            } finally { if(memory!=IntPtr.Zero) GlobalFree(memory); }
            return text;
        } finally { CloseClipboard(); }
    }
    internal static IntPtr KillOnCloseJob(Process process,bool allowBreakaway=false) {
        IntPtr job=CreateJobObject(IntPtr.Zero,null); if(job==IntPtr.Zero) throw new Win32Exception();
        var info=new ExtendedJob(); info.basic.flags=allowBreakaway?0x2800U:0x2000U;
        if(!SetInformationJobObject(job,9,ref info,(uint)Marshal.SizeOf(info))||!AssignProcessToJobObject(job,process.Handle)) { CloseHandle(job); throw new Win32Exception(); }
        return job;
    }
}
}
