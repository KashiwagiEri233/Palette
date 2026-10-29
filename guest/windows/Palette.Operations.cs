// Copyright Palette contributors. SPDX-License-Identifier: Apache-2.0
// All operations execute in the ordinary user's process, never in the service broker.
using System;
using System.Collections;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Automation;
using Microsoft.Win32;

namespace PaletteGuest {
internal sealed class Operations {
    internal static readonly HashSet<string> Capabilities = new HashSet<string>(new[] {
        "guest.input_text", "guest.filesystem", "guest.powershell", "guest.process",
        "guest.registry", "guest.app", "guest.snapshot", "guest.batch", "guest.clipboard"
    });
    sealed class ElementRecord {
        internal AutomationElement Element;
        internal string RuntimeId, Name;
        internal int ProcessId, ControlType;
        internal System.Windows.Rect Bounds;
    }
    sealed class SnapshotRecord {
        internal DateTime Created;
        internal Dictionary<int, ElementRecord> Elements = new Dictionary<int, ElementRecord>();
    }
    readonly Dictionary<string, SnapshotRecord> snapshots = new Dictionary<string, SnapshotRecord>();
    readonly Dictionary<string, Tuple<IntPtr,int,long>> windows = new Dictionary<string, Tuple<IntPtr,int,long>>();
    internal bool MutationStarted;
    internal void ClearSnapshots() { snapshots.Clear(); windows.Clear(); }

    internal Dictionary<string,object> Execute(string capability, Dictionary<string,object> p) {
        MutationStarted=false;
        if ((capability=="guest.app"||capability=="guest.snapshot"||capability=="guest.batch"||capability=="guest.clipboard"||capability=="guest.input_text") && !Native.Interactive())
            throw new UnauthorizedAccessException("The interactive desktop is locked or unavailable");
        switch (capability) {
            case "guest.filesystem": return FileSystem(p);
            case "guest.powershell":
                Wire.Fields(p,"command","timeout_seconds","cwd");
                MutationStarted=true;
                return Shell(Required(p,"command",32768), Seconds(p), OptionalDirectory(p,"cwd"));
            case "guest.process": return Processes(p);
            case "guest.registry": return RegistryOperation(p);
            case "guest.app": return App(p);
            case "guest.snapshot":
                Wire.Fields(p,"max_elements");
                return Snapshot(Wire.Number(p,"max_elements",500,1,2000));
            case "guest.batch":
                Wire.Fields(p,"operation","snapshot_id","labels");
                if (Wire.Text(p,"operation")!="resolve_elements") throw new ArgumentException("Unknown batch operation");
                return Resolve(p);
            case "guest.clipboard":
                Wire.Fields(p,"operation","text");
                string operation=Wire.Text(p,"operation");
                if (operation!="get" && operation!="set") throw new ArgumentException("Unknown clipboard operation");
                string text=Wire.Text(p,"text");
                CheckText(text,49152);
                MutationStarted=operation=="set"; string content=Native.Clipboard(text,operation=="set");
                return operation=="get" ? Wire.Map("text",content) : Wire.Map("submitted",true);
            case "guest.input_text":
                Wire.Fields(p,"text","method","target");
                if (Wire.Text(p,"method")!="native_text" || Wire.Text(p,"target","focused")!="focused")
                    throw new ArgumentException("Only native_text into the focused control is supported");
                string input=Required(p,"text",49152); CheckText(input,49152); MutationStarted=true; Native.TypeUnicode(input);
                return Wire.Map("submitted",true);
            default: throw new ArgumentException("Unknown capability");
        }
    }
    static string Required(Dictionary<string,object> p,string key,int limit=4096) {
        string text=Wire.Text(p,key);
        if (text.Length==0 || text.Length>limit || text.IndexOf('\0')>=0) throw new ArgumentException("Invalid "+key);
        return text;
    }
    static void CheckText(string text,int limit) {
        if (text.IndexOf('\0')>=0 || Encoding.UTF8.GetByteCount(text)>limit) throw new ArgumentException("Text exceeds its limit or contains NUL");
    }
    static string GuestPath(string path) {
        if (path.IndexOf('\0')>=0 || path.Length==0 || path.Length>4096) throw new ArgumentException("Invalid guest path");
        // Device paths and drive-relative paths are not filesystem names in this API.
        if (path.StartsWith(@"\\?\") || path.StartsWith(@"\\.\") || Regex.IsMatch(path,@"^[A-Za-z]:[^\\/]"))
            throw new ArgumentException("Device and drive-relative paths are unsupported");
        if (!Path.IsPathRooted(path)) path=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),path);
        return Path.GetFullPath(path);
    }
    static string OptionalDirectory(Dictionary<string,object> p,string key) {
        string text=Wire.Text(p,key); if (text=="") return null;
        string path=GuestPath(text); if (!Directory.Exists(path)) throw new DirectoryNotFoundException("Guest working directory does not exist"); return path;
    }
    static Encoding TextEncoding(string name) {
        switch (name) {
            case "utf-8": return new UTF8Encoding(false,true);
            case "utf-16": return new UnicodeEncoding(false,true,true);
            case "ascii": return Encoding.GetEncoding("us-ascii",EncoderFallback.ExceptionFallback,DecoderFallback.ExceptionFallback);
            default: throw new ArgumentException("Unsupported encoding");
        }
    }
    static void NoReparse(string path) {
        string current=path;
        while (!String.IsNullOrEmpty(current)) {
            if ((File.Exists(current)||Directory.Exists(current)) && (File.GetAttributes(current)&FileAttributes.ReparsePoint)!=0)
                throw new ArgumentException("Recursive mutations do not follow reparse points");
            current=Path.GetDirectoryName(current);
        }
    }
    static Dictionary<string,object> Metadata(string path) {
        FileSystemInfo item=Directory.Exists(path) ? (FileSystemInfo)new DirectoryInfo(path) : new FileInfo(path);
        if (!item.Exists) throw new FileNotFoundException("Guest path does not exist");
        return Wire.Map("path",item.FullName,"name",item.Name,"type",item is DirectoryInfo?"directory":"file",
            "size",item is FileInfo?((FileInfo)item).Length:0,"modified_utc",item.LastWriteTimeUtc.ToString("o"),
            "created_utc",item.CreationTimeUtc.ToString("o"),"reparse_point",(item.Attributes&FileAttributes.ReparsePoint)!=0);
    }
    Dictionary<string,object> FileSystem(Dictionary<string,object> p) {
        Wire.Fields(p,"mode","path","destination","content","append","recursive","overwrite","pattern","offset","limit","encoding");
        string mode=Required(p,"mode"), path=GuestPath(Required(p,"path"));
        int limit=Wire.Number(p,"limit",200,1,2000),offset=Wire.Number(p,"offset",1,1,Int32.MaxValue);
        bool recursive=Wire.Flag(p,"recursive"), overwrite=Wire.Flag(p,"overwrite");
        Encoding encoding=TextEncoding(Wire.Text(p,"encoding","utf-8"));
        if (mode=="info") return Metadata(path);
        if (mode=="read") {
            var text=new StringBuilder(); int line=0,selected=0; bool truncated=false;
            using(var reader=new StreamReader(path,encoding,true)) {
                string value;
                // Read bounded chunks rather than ReadLine(), which can allocate an enormous line.
                while ((value=ReadBoundedLine(reader,49152))!=null) {
                    line++; if (line<offset) continue;
                    if (selected==limit || Encoding.UTF8.GetByteCount(text.ToString())+Encoding.UTF8.GetByteCount(value)>49152) { truncated=true; break; }
                    text.Append(value); selected++;
                }
            }
            return Wire.Map("path",path,"content",text.ToString(),"offset",offset,"lines",selected,"truncated",truncated);
        }
        if (mode=="write") {
            if (!p.ContainsKey("content")) throw new ArgumentException("content is required");
            string text=Wire.Text(p,"content"); CheckText(text,49152);
            encoding.GetByteCount(text); // Validate encoding before creating or truncating a file.
            MutationStarted=true;
            string parent=Path.GetDirectoryName(path); if (!Directory.Exists(parent)) Directory.CreateDirectory(parent);
            if (Wire.Flag(p,"append")) File.AppendAllText(path,text,encoding); else File.WriteAllText(path,text,encoding);
            return Wire.Map("path",path,"bytes",encoding.GetByteCount(text),"submitted",true);
        }
        if (mode=="list" || mode=="search") {
            if (!Directory.Exists(path)) throw new DirectoryNotFoundException("Guest directory does not exist");
            string pattern=Wire.Text(p,"pattern","*"); if(pattern.Length>256) throw new ArgumentException("Pattern is too long");
            if(pattern=="" || pattern.IndexOfAny(new[]{'\\','/'})>=0) throw new ArgumentException("Pattern must match a file name");
            var matcher=new Regex("^"+Regex.Escape(pattern).Replace(@"\*",".*").Replace(@"\?",".")+"$",RegexOptions.IgnoreCase|RegexOptions.CultureInvariant);
            var result=new List<object>(); var todo=new Queue<string>(); todo.Enqueue(path); bool truncated=false; int resultBytes=0,matched=0;
            int scanned=0; var watch=Stopwatch.StartNew();
            while(todo.Count>0 && !truncated) {
                foreach(string child in Directory.EnumerateFileSystemEntries(todo.Dequeue())) {
                    if(++scanned>20000 || watch.ElapsedMilliseconds>10000) { truncated=true; break; }
                    FileAttributes attributes=File.GetAttributes(child);
                    if(matcher.IsMatch(Path.GetFileName(child)) && ++matched>=offset) {
                        if(result.Count==limit) { truncated=true; break; }
                        var item=Metadata(child); resultBytes+=Encoding.UTF8.GetByteCount(new JavaScriptSerializer().Serialize(item));
                        if(resultBytes>700000) { truncated=true; break; } result.Add(item);
                    }
                    if(mode=="search" && (attributes&FileAttributes.Directory)!=0 && (attributes&FileAttributes.ReparsePoint)==0) todo.Enqueue(child);
                }
            }
            return Wire.Map("path",path,"items",result,"truncated",truncated,"scanned",scanned);
        }
        if (mode=="copy" || mode=="move") {
            string destination=GuestPath(Required(p,"destination")); NoReparse(path); NoReparse(destination);
            if(!Directory.Exists(path)&&!File.Exists(path)) throw new FileNotFoundException("Source does not exist");
            string a=path.TrimEnd('\\')+"\\",b=destination.TrimEnd('\\')+"\\";
            if(a.StartsWith(b,StringComparison.OrdinalIgnoreCase)||b.StartsWith(a,StringComparison.OrdinalIgnoreCase)) throw new ArgumentException("Source and destination overlap");
            MutationStarted=true;
            if(Directory.Exists(destination)||File.Exists(destination)) {
                if(!overwrite) throw new IOException("Destination exists; overwrite is required");
                if(Directory.Exists(destination)) throw new IOException("Directory replacement is unsupported; choose a new destination");
                File.Delete(destination);
            }
            string parent=Path.GetDirectoryName(destination); if(!Directory.Exists(parent)) Directory.CreateDirectory(parent);
            if(Directory.Exists(path)) {
                if(mode=="move") Directory.Move(path,destination); else CopyDirectory(path,destination,0);
            } else if(mode=="move") File.Move(path,destination); else File.Copy(path,destination,false);
            return Wire.Map("path",path,"destination",destination,"submitted",true);
        }
        if (mode=="delete") {
            NoReparse(path);
            if(String.Equals(path.TrimEnd('\\'),Path.GetPathRoot(path).TrimEnd('\\'),StringComparison.OrdinalIgnoreCase)) throw new ArgumentException("Cannot delete a drive root");
            MutationStarted=true;
            if(Directory.Exists(path)) DeleteDirectory(path,recursive,0); else File.Delete(path);
            return Wire.Map("path",path,"submitted",true);
        }
        throw new ArgumentException("Unsupported filesystem mode");
    }
    static string ReadBoundedLine(StreamReader reader,int limit) {
        var result=new StringBuilder(); int value;
        while((value=reader.Read())>=0) { result.Append((char)value); if(result.Length>limit) throw new ArgumentException("A line exceeds the 48 KiB read limit"); if(value=='\n') break; }
        return result.Length==0?null:result.ToString();
    }
    static void CopyDirectory(string source,string destination,int depth) {
        if(depth>64) throw new ArgumentException("Directory nesting exceeds the limit"); NoReparse(source); Directory.CreateDirectory(destination);
        foreach(string child in Directory.EnumerateFileSystemEntries(source)) {
            NoReparse(child); string target=Path.Combine(destination,Path.GetFileName(child));
            if(Directory.Exists(child)) CopyDirectory(child,target,depth+1); else File.Copy(child,target,false);
        }
    }
    static void DeleteDirectory(string path,bool recursive,int depth) {
        if(depth>64) throw new ArgumentException("Directory nesting exceeds the limit"); NoReparse(path);
        if(recursive) foreach(string child in Directory.EnumerateFileSystemEntries(path)) {
            NoReparse(child); if(Directory.Exists(child)) DeleteDirectory(child,true,depth+1); else File.Delete(child);
        }
        Directory.Delete(path,false);
    }
    static double Seconds(Dictionary<string,object> p) {
        object item; if(!p.TryGetValue("timeout_seconds",out item)) return 20;
        if(!(item is int||item is decimal||item is double)) throw new ArgumentException("Invalid timeout");
        double seconds=Convert.ToDouble(item,CultureInfo.InvariantCulture);
        if(Double.IsNaN(seconds)||Double.IsInfinity(seconds)||seconds<1||seconds>60) throw new ArgumentException("Timeout must be between 1 and 60 seconds"); return seconds;
    }
    sealed class OutputBuffer {
        readonly StringBuilder text=new StringBuilder(); internal bool Truncated;
        internal void Read(StreamReader reader) { char[] chunk=new char[1024]; int count; while((count=reader.Read(chunk,0,chunk.Length))>0) {
            lock(text) { int remaining=16384-text.Length; if(remaining>0) text.Append(chunk,0,Math.Min(remaining,count)); if(count>remaining) Truncated=true; }
        } }
        public override string ToString() { lock(text) return text.ToString(); }
    }
    internal static Dictionary<string,object> Shell(string command,double seconds,string cwd) {
        string shell=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),@"WindowsPowerShell\v1.0\powershell.exe");
        // Keep source off argv: Win32 bounds the entire command line to 32767 UTF-16 units.
        string bootstrap="[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false); [Console]::InputEncoding=New-Object Text.UTF8Encoding($false); & ([ScriptBlock]::Create([Console]::In.ReadToEnd()))";
        var start=new ProcessStartInfo(shell,"-NoLogo -NoProfile -NonInteractive -EncodedCommand "+Convert.ToBase64String(Encoding.Unicode.GetBytes(bootstrap)));
        start.UseShellExecute=false; start.CreateNoWindow=true; start.RedirectStandardOutput=true; start.RedirectStandardError=true; start.RedirectStandardInput=true;
        start.StandardOutputEncoding=Encoding.UTF8; start.StandardErrorEncoding=Encoding.UTF8;
        if(cwd!=null) start.WorkingDirectory=cwd;
        using(var process=new Process { StartInfo=start }) {
            process.Start(); IntPtr job=IntPtr.Zero;
            try {
                job=Native.KillOnCloseJob(process);
                var stdout=new OutputBuffer(); var stderr=new OutputBuffer();
                Task output=Task.Factory.StartNew(delegate { stdout.Read(process.StandardOutput); });
                Task error=Task.Factory.StartNew(delegate { stderr.Read(process.StandardError); });
                using(var input=new StreamWriter(process.StandardInput.BaseStream,new UTF8Encoding(false))) input.Write(command);
                bool exited=process.WaitForExit((int)(seconds*1000));
                if(!exited) { Native.CloseHandle(job); job=IntPtr.Zero; if(!process.WaitForExit(3000)) throw new TimeoutException("Could not stop timed-out guest command"); }
                if(!Task.WaitAll(new[]{output,error},3000)) throw new TimeoutException("Command output did not close");
                return Wire.Map("stdout",stdout.ToString(),"stderr",stderr.ToString(),"exit_code",process.ExitCode,"timed_out",!exited,"truncated",stdout.Truncated||stderr.Truncated,"effective_timeout_seconds",seconds);
            } finally { if(job!=IntPtr.Zero) Native.CloseHandle(job); try { if(!process.HasExited) process.Kill(); } catch { } }
        }
    }
    Dictionary<string,object> Processes(Dictionary<string,object> p) {
        Wire.Fields(p,"mode","name","pid","sort_by","limit","force");
        string mode=Required(p,"mode"),name=Wire.Text(p,"name"); int pid=Wire.Number(p,"pid",0,1,Int32.MaxValue);
        if(mode=="kill") {
            if((pid==0)==(name=="")) throw new ArgumentException("kill requires exactly one of pid or name");
            Process[] matches=pid!=0?new[]{Process.GetProcessById(pid)}:Process.GetProcessesByName(name.EndsWith(".exe",StringComparison.OrdinalIgnoreCase)?name.Substring(0,name.Length-4):name);
            try {
                if(matches.Length!=1) throw new ArgumentException("Process name must identify exactly one process; use PID");
                Process target=matches[0]; int id=target.Id;
                MutationStarted=true;
                if(Wire.Flag(p,"force")) target.Kill(); else if(!target.CloseMainWindow()) throw new InvalidOperationException("Process has no closeable window; force is required");
                return Wire.Map("pid",id,"submitted",true,"exited",target.WaitForExit(1000));
            } finally { foreach(Process target in matches) target.Dispose(); }
        }
        if(mode!="list") throw new ArgumentException("Unsupported process mode");
        var items=new List<Dictionary<string,object>>();
        foreach(Process process in Process.GetProcesses()) using(process) {
            try {
                if(name!=""&&process.ProcessName.IndexOf(name,StringComparison.OrdinalIgnoreCase)<0) continue;
                items.Add(Wire.Map("pid",process.Id,"name",process.ProcessName,"memory",process.WorkingSet64,"cpu",process.TotalProcessorTime.TotalSeconds));
            } catch(Win32Exception) { } catch(InvalidOperationException) { }
        }
        string sort=Wire.Text(p,"sort_by","memory"); if(sort!="memory"&&sort!="cpu"&&sort!="name") throw new ArgumentException("Invalid process sort");
        items.Sort(delegate(Dictionary<string,object> a,Dictionary<string,object> b) { return sort=="name"?String.Compare((string)a[sort],(string)b[sort],StringComparison.OrdinalIgnoreCase):Convert.ToDouble(b[sort]).CompareTo(Convert.ToDouble(a[sort])); });
        int limit=Wire.Number(p,"limit",20,1,2000); bool truncated=items.Count>limit;
        return Wire.Map("items",items.Take(limit).ToArray(),"truncated",truncated);
    }
    Dictionary<string,object> RegistryOperation(Dictionary<string,object> p) {
        Wire.Fields(p,"mode","path","name","value","type");
        string mode=Required(p,"mode"),path=Required(p,"path"),name=p.ContainsKey("name")?Wire.Text(p,"name"):null;
        int separator=path.IndexOf(':'); if(separator<0) throw new ArgumentException("Registry path must begin HKCU: or HKLM:");
        string root=path.Substring(0,separator).ToUpperInvariant(),subkey=path.Substring(separator+1).TrimStart('\\','/');
        RegistryHive hive; switch(root) {
            case "HKCU": case "HKEY_CURRENT_USER": hive=RegistryHive.CurrentUser; break;
            case "HKLM": case "HKEY_LOCAL_MACHINE": hive=RegistryHive.LocalMachine; break;
            case "HKCR": hive=RegistryHive.ClassesRoot; break;
            case "HKU": hive=RegistryHive.Users; break;
            case "HKCC": hive=RegistryHive.CurrentConfig; break;
            default: throw new ArgumentException("Unsupported registry hive");
        }
        using(RegistryKey baseKey=RegistryKey.OpenBaseKey(hive,RegistryView.Registry64)) {
            if(mode=="delete"&&name==null) {
                if(subkey=="") throw new ArgumentException("Cannot delete a registry hive"); MutationStarted=true; baseKey.DeleteSubKeyTree(subkey,true); return Wire.Map("submitted",true);
            }
            bool writable=mode=="set"||mode=="delete";
            MutationStarted=writable;
            using(RegistryKey key=mode=="set"?baseKey.CreateSubKey(subkey):baseKey.OpenSubKey(subkey,writable)) {
                if(key==null) throw new ArgumentException("Registry key does not exist");
                if(mode=="list") {
                    var values=new List<object>(); int resultBytes=0;
                    foreach(string valueName in key.GetValueNames().Take(200)) {
                        var item=RegistryValue(key,valueName); resultBytes+=Encoding.UTF8.GetByteCount(new JavaScriptSerializer().Serialize(item));
                        if(resultBytes>650000) break; values.Add(item);
                    }
                    string[] children=key.GetSubKeyNames(); return Wire.Map("values",values,"subkeys",children.Take(200).ToArray(),"truncated",key.ValueCount>values.Count||children.Length>200);
                }
                if(name==null) throw new ArgumentException("name is required");
                if(mode=="get") return RegistryValue(key,name);
                if(mode=="delete") { key.DeleteValue(name,true); return Wire.Map("submitted",true); }
                if(mode!="set") throw new ArgumentException("Unsupported registry mode");
                object value; if(!p.TryGetValue("value",out value)) throw new ArgumentException("value is required");
                RegistryValueKind kind; if(!Enum.TryParse<RegistryValueKind>(Wire.Text(p,"type","String"),false,out kind)) throw new ArgumentException("Unsupported registry type");
                switch(kind) {
                    case RegistryValueKind.String: case RegistryValueKind.ExpandString:
                        if(!(value is string)) throw new ArgumentException("Expected string registry value"); CheckText((string)value,49152); break;
                    case RegistryValueKind.DWord: value=unchecked((int)Convert.ToUInt32(value,CultureInfo.InvariantCulture)); break;
                    case RegistryValueKind.QWord: value=unchecked((long)Convert.ToUInt64(value,CultureInfo.InvariantCulture)); break;
                    case RegistryValueKind.Binary:
                        object[] bytes=Wire.Array(p,"value"); if(bytes.Length>4096) throw new ArgumentException("Binary registry value is too large"); value=bytes.Select(item=>Convert.ToByte(item,CultureInfo.InvariantCulture)).ToArray(); break;
                    case RegistryValueKind.MultiString:
                        object[] strings=Wire.Array(p,"value"); if(strings.Length>4096||strings.Any(item=>!(item is string))) throw new ArgumentException("Expected string array");
                        CheckText(String.Join("",strings.Cast<string>()),49152); value=strings.Cast<string>().ToArray(); break;
                    default: throw new ArgumentException("Unsupported registry type");
                }
                key.SetValue(name,value,kind); return Wire.Map("submitted",true);
            }
        }
    }
    static Dictionary<string,object> RegistryValue(RegistryKey key,string name) {
        if(!key.GetValueNames().Contains(name,StringComparer.OrdinalIgnoreCase)) throw new ArgumentException("Registry value does not exist");
        RegistryValueKind kind=key.GetValueKind(name); object value=key.GetValue(name,null,RegistryValueOptions.DoNotExpandEnvironmentNames);
        if(kind==RegistryValueKind.Binary) value=((byte[])value).Select(item=>(int)item).ToArray();
        if(kind==RegistryValueKind.DWord) value=unchecked((uint)(int)value);
        if(kind==RegistryValueKind.QWord) {
            ulong number=unchecked((ulong)(long)value);
            // Preserve exact values through JavaScript MCP clients as well as Python.
            value=number>9007199254740991UL?(object)number.ToString(CultureInfo.InvariantCulture):number;
        }
        if(new JavaScriptSerializer().Serialize(value).Length>49152) throw new ArgumentException("Registry value exceeds the read limit");
        return Wire.Map("name",name,"type",kind.ToString(),"value",value);
    }
    List<Dictionary<string,object>> WindowList() {
        windows.Clear(); var result=new List<Dictionary<string,object>>();
        Native.EnumWindows(delegate(IntPtr hwnd,IntPtr state) {
            if(!Native.IsWindowVisible(hwnd)) return true;
            string title=Native.Title(hwnd); if(title=="") return true;
            uint pid; Native.GetWindowThreadProcessId(hwnd,out pid); Native.Rect rect;
            if(!Native.GetWindowRect(hwnd,out rect)) return true;
            long stamp; try { using(var process=Process.GetProcessById((int)pid)) stamp=process.StartTime.ToUniversalTime().Ticks; } catch { return true; }
            string id=hwnd.ToInt64().ToString("x")+"-"+pid+"-"+stamp;
            windows[id]=Tuple.Create(hwnd,(int)pid,stamp);
            result.Add(Wire.Map("window_id",id,"name",Limit(title,128),"pid",pid,"active",hwnd==Native.GetForegroundWindow(),"bounds",Bounds(rect.Left,rect.Top,rect.Right,rect.Bottom)));
            return result.Count<200;
        },IntPtr.Zero);
        return result;
    }
    static Dictionary<string,object> Bounds(int left,int top,int right,int bottom) {
        return Wire.Map("left",left,"top",top,"right",right,"bottom",bottom,"width",right-left,"height",bottom-top);
    }
    Dictionary<string,object> App(Dictionary<string,object> p) {
        Wire.Fields(p,"mode","name","executable","args","cwd","window_id","window_loc","window_size");
        string mode=Wire.Text(p,"mode","launch");
        if(mode=="launch_executable") {
            string executable=GuestPath(Required(p,"executable")); if(!File.Exists(executable)||!executable.EndsWith(".exe",StringComparison.OrdinalIgnoreCase)) throw new ArgumentException("An existing .exe path is required");
            object[] args=Wire.Array(p,"args"); if(args.Length>64||args.Any(item=>!(item is string))) throw new ArgumentException("Invalid argv");
            string arguments=String.Join(" ",args.Cast<string>().Select(Native.Quote)); CheckText(arguments,49152);
            string cwd=OptionalDirectory(p,"cwd")??Path.GetDirectoryName(executable);
            MutationStarted=true;
            return Wire.Map("pid",Native.StartDetachedApplication(executable,arguments,cwd),"submitted",true);
        }
        if(mode=="launch") {
            string name=Required(p,"name");
            string script="$n=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"+Convert.ToBase64String(Encoding.UTF8.GetBytes(name))+"')); @(Get-StartApps | Where-Object {$_.Name -eq $n} | Select-Object Name,AppID) | ConvertTo-Json -Compress";
            var listed=Shell(script,10,null);
            if((bool)listed["timed_out"]||(bool)listed["truncated"]||Convert.ToInt32(listed["exit_code"])!=0) throw new InvalidOperationException("Start Menu enumeration failed");
            string raw=((string)listed["stdout"]).Trim(); if(raw=="") throw new ArgumentException("No Start Menu application matches that name");
            object parsed=new JavaScriptSerializer().DeserializeObject(raw); var matches=parsed as object[]??new[]{parsed};
            if(matches.Length!=1) throw new ArgumentException("Application name is ambiguous; specify an executable path");
            string appId=Wire.Text(Wire.Object(matches[0]),"AppID");
            if(appId.IndexOf('\0')>=0) throw new ArgumentException("Invalid application id");
            MutationStarted=true;
            Native.StartDetachedApplication(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows),"explorer.exe"),Native.Quote("shell:AppsFolder\\"+appId),null);
            return Wire.Map("app_id",appId,"submitted",true);
        }
        if(mode!="switch"&&mode!="resize") throw new ArgumentException("Unsupported App mode");
        string id=Wire.Text(p,"window_id"),targetName=Wire.Text(p,"name");
        List<Dictionary<string,object>> current=WindowList(); IntPtr target;
        if(id!="") { Tuple<IntPtr,int,long> record; if(!windows.TryGetValue(id,out record)) throw new ArgumentException("Window is no longer available"); target=record.Item1; }
        else if(targetName!="") {
            var matches=current.Where(item=>String.Equals((string)item["name"],targetName,StringComparison.OrdinalIgnoreCase)).ToArray();
            if(matches.Length!=1) throw new ArgumentException("Window name must match exactly one window; use its window_id"); target=windows[(string)matches[0]["window_id"]].Item1;
        } else target=Native.GetForegroundWindow();
        if(!Native.IsWindow(target)) throw new ArgumentException("No target window is available");
        MutationStarted=true;
        if(mode=="switch") {
            if(!Native.FocusWindow(target)) throw new InvalidOperationException("Windows did not grant foreground focus");
        } else {
            if(Native.IsIconic(target)||Native.IsZoomed(target)) Native.ShowWindow(target,9);
            Native.Rect rect; if(!Native.GetWindowRect(target,out rect)) throw new Win32Exception();
            object[] loc=Wire.Array(p,"window_loc"),size=Wire.Array(p,"window_size");
            if(loc.Length!=0&&loc.Length!=2||size.Length!=0&&size.Length!=2) throw new ArgumentException("Window geometry requires two integers");
            int x=loc.Length==0?rect.Left:Convert.ToInt32(loc[0]),y=loc.Length==0?rect.Top:Convert.ToInt32(loc[1]);
            int width=size.Length==0?rect.Right-rect.Left:Convert.ToInt32(size[0]),height=size.Length==0?rect.Bottom-rect.Top:Convert.ToInt32(size[1]);
            if(width<1||height<1||width>32768||height>32768) throw new ArgumentException("Invalid window size");
            if(!Native.SetWindowPos(target,IntPtr.Zero,x,y,width,height,0x14)) throw new Win32Exception();
        }
        Native.Rect actual; if(!Native.GetWindowRect(target,out actual)) throw new Win32Exception();
        return Wire.Map("submitted",true,"active",Native.GetForegroundWindow()==target,"bounds",Bounds(actual.Left,actual.Top,actual.Right,actual.Bottom));
    }
    Dictionary<string,object> Snapshot(int maximum) {
        ClearSnapshots(); var record=new SnapshotRecord { Created=DateTime.UtcNow };
        var items=new List<object>(); var todo=new Queue<AutomationElement>(); var watch=Stopwatch.StartNew();
        todo.Enqueue(AutomationElement.RootElement); bool truncated=false; int visited=0,resultBytes=0;
        while(todo.Count>0) {
            if(watch.ElapsedMilliseconds>5000||visited++>10000||items.Count>=maximum) { truncated=true; break; }
            AutomationElement element=todo.Dequeue();
            try {
                var info=element.Current; var rect=info.BoundingRectangle;
                if(!info.IsOffscreen && info.IsEnabled && rect.Width>0 && rect.Height>0 &&
                    (info.IsKeyboardFocusable || element.GetSupportedPatterns().Any(pattern=>pattern==InvokePattern.Pattern||pattern==ValuePattern.Pattern||pattern==SelectionItemPattern.Pattern||pattern==TogglePattern.Pattern||pattern==ScrollPattern.Pattern))) {
                    int label=items.Count;
                    record.Elements[label]=new ElementRecord { Element=element,RuntimeId=String.Join(".",element.GetRuntimeId()),Name=info.Name,
                        ProcessId=info.ProcessId,ControlType=info.ControlType.Id,Bounds=rect };
                    var item=Wire.Map("label",label,"name",info.IsPassword?"":Limit(info.Name,256),"control_type",info.ControlType.ProgrammaticName,
                        "pid",info.ProcessId,"focused",info.HasKeyboardFocus,"password",info.IsPassword,
                        "bounds",Bounds((int)rect.Left,(int)rect.Top,(int)rect.Right,(int)rect.Bottom));
                    resultBytes+=Encoding.UTF8.GetByteCount(new JavaScriptSerializer().Serialize(item));
                    if(resultBytes>700000) { record.Elements.Remove(label); truncated=true; break; } items.Add(item);
                }
                // ControlView avoids Chrome's enormous raw accessibility subtree.
                var walker=TreeWalker.ControlViewWalker; AutomationElement child=walker.GetFirstChild(element);
                while(child!=null&&todo.Count<10000) { todo.Enqueue(child); child=walker.GetNextSibling(child); }
            } catch(ElementNotAvailableException) { } catch(InvalidOperationException) { } catch(COMException) { }
        }
        string id=Guid.NewGuid().ToString("N"); snapshots[id]=record;
        int left=Native.GetSystemMetrics(76),top=Native.GetSystemMetrics(77),width=Native.GetSystemMetrics(78),height=Native.GetSystemMetrics(79);
        return Wire.Map("snapshot_id",id,"elements",items,"windows",WindowList(),"truncated",truncated,
            "virtual_screen",Bounds(left,top,left+width,top+height));
    }
    Dictionary<string,object> Resolve(Dictionary<string,object> p) {
        SnapshotRecord record; if(!snapshots.TryGetValue(Required(p,"snapshot_id",128),out record)||(DateTime.UtcNow-record.Created).TotalSeconds>30) throw new ArgumentException("Snapshot is stale; inspect again");
        object[] labels=Wire.Array(p,"labels"); if(labels.Length<1||labels.Length>64) throw new ArgumentException("Invalid label count");
        var points=new List<object>();
        foreach(object value in labels) {
            if(!(value is int)) throw new ArgumentException("Labels must be integers");
            ElementRecord target; if(!record.Elements.TryGetValue((int)value,out target)) throw new ArgumentException("Label does not belong to the snapshot");
            var info=target.Element.Current; var rect=info.BoundingRectangle;
            if(info.IsOffscreen||!info.IsEnabled||info.ProcessId!=target.ProcessId||info.ControlType.Id!=target.ControlType||
                info.Name!=target.Name||String.Join(".",target.Element.GetRuntimeId())!=target.RuntimeId||rect!=target.Bounds)
                throw new ArgumentException("Target changed after the snapshot");
            points.Add(new[]{(int)(rect.Left+rect.Width/2),(int)(rect.Top+rect.Height/2)});
        }
        return Wire.Map("points",points);
    }
    static string Limit(string value,int length) { return value.Length<=length?value:value.Substring(0,length); }
}
}
