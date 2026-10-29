// Copyright Palette contributors. SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Security.AccessControl;
using System.Security.Principal;
using System.ServiceProcess;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace PaletteGuest {
internal static class Wire {
    internal const int Maximum=1048576;
    internal static Dictionary<string,object> Map(params object[] pairs) {
        var value=new Dictionary<string,object>(); for(int i=0;i<pairs.Length;i+=2) value[(string)pairs[i]]=pairs[i+1]; return value;
    }
    internal static Dictionary<string,object> Object(object value) { var result=value as Dictionary<string,object>; if(result==null) throw new ArgumentException("Expected an object"); return result; }
    internal static string Text(Dictionary<string,object> value,string name,string fallback="") { object item; if(!value.TryGetValue(name,out item)) return fallback; if(!(item is string)) throw new ArgumentException(name+" must be text"); return (string)item; }
    internal static int Number(Dictionary<string,object> value,string name,int fallback,int minimum,int maximum) {
        object item; if(!value.TryGetValue(name,out item)) return fallback;
        if(!(item is int)|| (int)item<minimum || (int)item>maximum) throw new ArgumentException(name+" is outside its integer range"); return (int)item;
    }
    internal static bool Flag(Dictionary<string,object> value,string name,bool fallback=false) { object item; if(!value.TryGetValue(name,out item)) return fallback; if(!(item is bool)) throw new ArgumentException(name+" must be boolean"); return (bool)item; }
    internal static object[] Array(Dictionary<string,object> value,string name) { object item; if(!value.TryGetValue(name,out item)) return new object[0]; var a=item as object[]; if(a!=null) return a; var list=item as ArrayList; if(list!=null) return list.ToArray(); throw new ArgumentException(name+" must be an array"); }
    internal static void Fields(Dictionary<string,object> value,params string[] names) { var allowed=new HashSet<string>(names); foreach(string name in value.Keys) if(!allowed.Contains(name)) throw new ArgumentException("Unexpected parameter: "+name); }
    internal static Dictionary<string,object> Read(Stream stream) {
        byte[] prefix=ReadExactly(stream,4); int size=(prefix[0]<<24)|(prefix[1]<<16)|(prefix[2]<<8)|prefix[3];
        if(size<2||size>Maximum) throw new IOException("Invalid frame length");
        string json=new UTF8Encoding(false,true).GetString(ReadExactly(stream,size));
        return Object(new JavaScriptSerializer{MaxJsonLength=Maximum,RecursionLimit=32}.DeserializeObject(json));
    }
    static byte[] ReadExactly(Stream stream,int size) { byte[] result=new byte[size]; int offset=0; while(offset<size) { int read=stream.Read(result,offset,size-offset); if(read==0) throw new EndOfStreamException(); offset+=read; } return result; }
    internal static void Write(Stream stream,Dictionary<string,object> message) {
        byte[] data=Encoding.UTF8.GetBytes(new JavaScriptSerializer{MaxJsonLength=Maximum,RecursionLimit=32}.Serialize(message));
        if(data.Length>Maximum) throw new IOException("Result exceeds the protocol size limit");
        byte[] prefix={(byte)(data.Length>>24),(byte)(data.Length>>16),(byte)(data.Length>>8),(byte)data.Length}; stream.Write(prefix,0,4); stream.Write(data,0,data.Length); stream.Flush();
    }
    internal static Dictionary<string,object> Reply(Dictionary<string,object> request,Dictionary<string,object> payload,string kind="response") { return Map("v",Text(request,"v","1.1"),"kind",kind,"id",Text(request,"id"),"payload",payload); }
    internal static Dictionary<string,object> Failure(Dictionary<string,object> request,string message,string effect="not_sent",bool reconnect=false) { return Reply(request,Map("ok",false,"effect_state",effect,"detail",message,"data",reconnect?Map("reconnect_required",true):Map())); }
    internal static void Log(string message) {
        try { string dir=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),"Palette","GuestTools","Logs"); Directory.CreateDirectory(dir); string path=Path.Combine(dir,"palette-agent.log"); if(File.Exists(path)&&new FileInfo(path).Length>1048576) File.Delete(path); File.AppendAllText(path,DateTime.UtcNow.ToString("o")+" "+message+Environment.NewLine); } catch { }
    }
}

internal sealed class Broker : ServiceBase {
    volatile bool stopping;
    FileStream port;
    NamedPipeServerStream workerPipe;
    Process worker;
    IntPtr workerJob=IntPtr.Zero;
    uint workerSession=UInt32.MaxValue;
    Dictionary<string,object> hello;
    string activeId, activeConnect;
    volatile bool activeCancelled;
    readonly object workerLock=new object(),writeLock=new object();
    readonly SemaphoreSlim operation=new SemaphoreSlim(1,1);
    Thread reader;
    internal Broker() { ServiceName="PaletteGuestTools"; CanStop=true; AutoLog=false; }
    protected override void OnStart(string[] args) { reader=new Thread(Run); reader.IsBackground=true; reader.Start(); }
    protected override void OnStop() { stopping=true; DropWorker(); var connection=port; if(connection!=null) connection.Dispose(); if(reader!=null) reader.Join(3000); }
    void Run() {
        while(!stopping) {
            try {
                using(var connection=Native.OpenPort()) {
                    port=connection;
                    while(!stopping) {
                        var request=Wire.Read(connection);
                        string kind=Wire.Text(request,"kind");
                        if(kind=="cancel") { var payload=Wire.Object(request["payload"]); if(Wire.Text(request,"id")==activeId&&Wire.Text(payload,"connect_id")==activeConnect) { activeCancelled=true; DropWorker(); } continue; }
                        string requestId=Wire.Text(request,"id"),connectId=Wire.Text(Wire.Object(request["payload"]),"connect_id");
                        if(!operation.Wait(0)) { Write(connection,Wire.Failure(request,"Another guest operation is in progress")); continue; }
                        // Record identity before queuing: an immediately following cancel must
                        // also cancel a request whose dispatch thread has not started yet.
                        activeId=requestId; activeConnect=connectId; activeCancelled=false;
                        Task.Factory.StartNew(delegate { try { Dispatch(connection,request); } catch(Exception e) { Wire.Log("Broker request failed: "+e.GetType().Name); } finally { activeId=activeConnect=null; operation.Release(); } });
                    }
                }
            } catch(Exception e) { if(!stopping) { Wire.Log("Guest transport reconnect: "+e.GetType().Name); Thread.Sleep(1000); } }
            finally { port=null; hello=null; DropWorker(); }
        }
    }
    void Write(Stream connection,Dictionary<string,object> value) { lock(writeLock) { try { Wire.Write(connection,value); } catch(IOException) { } catch(ObjectDisposedException) { } } }
    void Dispatch(Stream connection,Dictionary<string,object> request) {
        string kind=Wire.Text(request,"kind");
        if(kind!="hello"&&kind!="request") { Write(connection,Wire.Failure(request,"Unsupported message kind")); return; }
        var payload=Wire.Object(request["payload"]);
        if(activeCancelled) { Write(connection,Wire.Failure(request,"Guest request cancelled before execution")); return; }
        int timeout=kind=="hello"?10000:Wire.Number(payload,"timeout_ms",20000,1,65000);
        if(kind=="hello") { hello=request; DropWorker(); }
        if(kind!="hello"&&hello==null) { Write(connection,Wire.Failure(request,"Guest service restarted; a new handshake is required","not_sent",true)); return; }
        uint session=Native.WTSGetActiveConsoleSessionId();
        if(session==UInt32.MaxValue) {
            DropWorker();
            Write(connection,kind=="hello"?Wire.Reply(request,Wire.Map("version","1.1","capabilities",new string[0],"input_methods",new string[0],"apps",new string[0],"session","interactive","interactive_desktop",false,"detail","No active console user is logged in"),"hello_ack"):Wire.Failure(request,"No active console user is logged in","not_sent",true)); return;
        }
        bool sent=false;
        try {
            NamedPipeServerStream pipe=EnsureWorker(session);
            if(activeCancelled) { Write(connection,Wire.Failure(request,"Guest request cancelled before execution")); return; }
            if(kind!="hello"&&hello==null) throw new InvalidOperationException("A hello handshake is required");
            sent=true;
            var response=Task.Factory.StartNew(delegate { Wire.Write(pipe,request); return Wire.Read(pipe); });
            if(!response.Wait(timeout)) { DropWorker(); Write(connection,Wire.Failure(request,"Guest operation exceeded its deadline; worker stopped, effects may be partial","unknown")); return; }
            Write(connection,response.Result);
        } catch(Exception e) { DropWorker(); Write(connection,Wire.Failure(request,"Guest worker unavailable: "+e.GetType().Name,sent?"unknown":"not_sent")); }
    }
    NamedPipeServerStream EnsureWorker(uint session) {
        lock(workerLock) {
            if(worker!=null&&!worker.HasExited&&workerSession==session&&workerPipe!=null&&workerPipe.IsConnected) return workerPipe;
            DropWorkerLocked();
            string name="palette-tools-"+Guid.NewGuid().ToString("N");
            SecurityIdentifier sid=Native.SessionUser(session);
            var security=new PipeSecurity(); security.SetAccessRuleProtection(true,false);
            security.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(WellKnownSidType.LocalSystemSid,null),PipeAccessRights.FullControl,AccessControlType.Allow));
            security.AddAccessRule(new PipeAccessRule(sid,PipeAccessRights.ReadWrite,AccessControlType.Allow));
            workerPipe=new NamedPipeServerStream(name,PipeDirection.InOut,1,PipeTransmissionMode.Byte,PipeOptions.Asynchronous,65536,65536,security);
            var wait=workerPipe.BeginWaitForConnection(null,null);
            worker=Native.StartUserWorker(session,Process.GetCurrentProcess().MainModule.FileName,name,out sid); workerSession=session;
            workerJob=Native.KillOnCloseJob(worker,true);
            if(!wait.AsyncWaitHandle.WaitOne(10000)) { DropWorkerLocked(); throw new System.TimeoutException("Interactive worker did not connect"); }
            workerPipe.EndWaitForConnection(wait); wait.AsyncWaitHandle.Close();
            uint client; if(!Native.GetNamedPipeClientProcessId(workerPipe.SafePipeHandle,out client)||client!=worker.Id) { DropWorkerLocked(); throw new UnauthorizedAccessException("Worker identity mismatch"); }
            // After a cancelled request, establish a fresh worker session before the next request.
            if(hello!=null&&Wire.Text(hello,"id")!=activeId) { Wire.Write(workerPipe,hello); var ack=Wire.Read(workerPipe); if(Wire.Text(ack,"kind")!="hello_ack") throw new IOException("Worker handshake failed"); }
            return workerPipe;
        }
    }
    void DropWorker() { lock(workerLock) DropWorkerLocked(); }
    void DropWorkerLocked() {
        var pipe=workerPipe; workerPipe=null; if(pipe!=null) pipe.Dispose();
        if(workerJob!=IntPtr.Zero) { Native.CloseHandle(workerJob); workerJob=IntPtr.Zero; }
        var process=worker; worker=null; if(process!=null) { try { if(!process.HasExited) { process.Kill(); process.WaitForExit(1000); } } catch { } process.Dispose(); }
        workerSession=UInt32.MaxValue;
    }
}

internal static class Program {
    static int Main(string[] args) {
        try {
            if(args.Length==2&&args[0]=="--worker") {
                // Never permit a misconfigured service launch to execute operations as SYSTEM.
                using(var identity=WindowsIdentity.GetCurrent()) if(identity.IsSystem) throw new UnauthorizedAccessException("Worker must run as the console user");
                using(var pipe=new NamedPipeClientStream(".",args[1],PipeDirection.InOut,PipeOptions.None,TokenImpersonationLevel.Identification)) { pipe.Connect(10000); WorkerLoop(pipe,pipe); }
            } else if(args.Length==1&&args[0]=="--stdio") {
                // Diagnostic/test transport runs solely with the invoking user's existing rights.
                using(var identity=WindowsIdentity.GetCurrent()) if(identity.IsSystem) throw new UnauthorizedAccessException("Worker must not be SYSTEM");
                WorkerLoop(Console.OpenStandardInput(),Console.OpenStandardOutput());
            } else ServiceBase.Run(new Broker());
            return 0;
        } catch(Exception e) { Wire.Log("Agent stopped: "+e.GetType().Name); return 1; }
    }
    static void WorkerLoop(Stream input,Stream output) {
        Native.DpiAware(); var operations=new Operations(); string connection=null; int boot=0,generation=-1; var grants=new HashSet<string>();
        while(true) {
            Dictionary<string,object> request;
            try { request=Wire.Read(input); } catch(EndOfStreamException) { return; }
            Dictionary<string,object> reply;
            operations.MutationStarted=false;
            try {
                var payload=Wire.Object(request["payload"]); string kind=Wire.Text(request,"kind");
                if(kind=="hello") {
                    var versions=Wire.Array(payload,"versions"); if(Array.IndexOf(versions,"1.1")<0) throw new ArgumentException("Protocol 1.1 is required");
                    connection=Wire.Text(payload,"connect_id"); boot=Wire.Number(payload,"boot_epoch",0,0,Int32.MaxValue); generation=-1; operations.ClearSnapshots(); grants.Clear();
                    foreach(object capability in Wire.Array(payload,"desired_capabilities")) if(capability is string&&Operations.Capabilities.Contains((string)capability)) grants.Add((string)capability);
                    reply=Wire.Reply(request,Wire.Map("version","1.1","capabilities",new List<string>(grants),"input_methods",new[]{"native_text"},"apps",new string[0],"session","interactive","interactive_desktop",Native.Interactive(),"detail","Palette Windows user-session tools"),"hello_ack");
                } else {
                    if(kind!="request"||connection==null||Wire.Text(payload,"connect_id")!=connection||Wire.Number(payload,"boot_epoch",-1,0,Int32.MaxValue)!=boot) throw new ArgumentException("Stale guest session");
                    int current=Wire.Number(payload,"generation",0,0,Int32.MaxValue); if(generation<0) generation=current; if(generation!=current) throw new ArgumentException("Stale connection generation");
                    string capability=Wire.Text(payload,"capability"); if(!grants.Contains(capability)) throw new UnauthorizedAccessException("Capability was not granted");
                    var result=operations.Execute(capability,Wire.Object(payload["params"]));
                    reply=Wire.Reply(request,Wire.Map("ok",true,"effect_state","submitted","method",capability,"data",result));
                }
            } catch(ArgumentException e) { reply=Wire.Failure(request,e.Message,operations.MutationStarted?"unknown":"not_sent"); }
              catch(UnauthorizedAccessException e) { reply=Wire.Failure(request,e.Message,operations.MutationStarted?"unknown":"not_sent"); }
              catch(Exception e) { reply=Wire.Failure(request,e.Message,operations.MutationStarted?"unknown":"not_sent"); }
            try { Wire.Write(output,reply); } catch(IOException) { return; }
        }
    }
}
}
