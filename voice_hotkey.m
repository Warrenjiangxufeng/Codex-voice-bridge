#import <Cocoa/Cocoa.h>
#import <Carbon/Carbon.h>
#import <ApplicationServices/ApplicationServices.h>
#import <signal.h>
#import <unistd.h>

static pid_t parentPid = 0;
static CFAbsoluteTime lastToggle = 0;
static CFAbsoluteTime lastEnterDown = 0;

static void emitLine(const char *line) {
    printf("%s\n", line);
    fflush(stdout);
}

static void toggleRecording(void) {
    CFAbsoluteTime now = CFAbsoluteTimeGetCurrent();
    if (now - lastToggle < 0.35) {
        return;
    }
    lastToggle = now;
    emitLine("TOGGLE");
}

static OSStatus hotKeyHandler(EventHandlerCallRef nextHandler,
                              EventRef event,
                              void *userData) {
    toggleRecording();
    return noErr;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc > 1) {
            parentPid = (pid_t)atoi(argv[1]);
        }

        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
        [NSApp finishLaunching];

        EventTypeSpec eventType = {
            .eventClass = kEventClassKeyboard,
            .eventKind = kEventHotKeyPressed
        };
        EventHandlerRef handlerRef = NULL;
        OSStatus installStatus = InstallEventHandler(
            GetApplicationEventTarget(),
            NewEventHandlerUPP(hotKeyHandler),
            1,
            &eventType,
            NULL,
            &handlerRef
        );
        if (installStatus != noErr) {
            char line[64];
            snprintf(line, sizeof(line), "ERROR:install:%d", (int)installStatus);
            emitLine(line);
            return 2;
        }

        EventHotKeyID fallbackId = {
            .signature = 'LISA',
            .id = 2
        };
        EventHotKeyRef fallbackRef = NULL;
        OSStatus fallbackStatus = RegisterEventHotKey(
            (UInt32)kVK_Space,
            (UInt32)(controlKey | optionKey),
            fallbackId,
            GetApplicationEventTarget(),
            0,
            &fallbackRef
        );

        BOOL canListen = CGPreflightListenEventAccess();
        id monitor = [NSEvent addGlobalMonitorForEventsMatchingMask:NSEventMaskKeyDown
                                                           handler:^(NSEvent *event) {
            if (event.isARepeat) {
                return;
            }
            BOOL isEnter = event.keyCode == (unsigned short)kVK_Return ||
                           event.keyCode == (unsigned short)kVK_ANSI_KeypadEnter;
            if (!isEnter) {
                return;
            }
            CFAbsoluteTime now = CFAbsoluteTimeGetCurrent();
            if (now - lastEnterDown <= 0.45) {
                lastEnterDown = 0;
                toggleRecording();
            } else {
                lastEnterDown = now;
            }
        }];

        if (!canListen) {
            CGRequestListenEventAccess();
            if (fallbackStatus == noErr) {
                emitLine("NEEDS_PERMISSION:FALLBACK_OK");
            } else {
                emitLine("NEEDS_PERMISSION");
            }
        } else if (monitor != nil) {
            emitLine("READY:DOUBLE_ENTER");
        } else if (fallbackStatus == noErr) {
            emitLine("READY:FALLBACK");
        } else {
            char line[64];
            snprintf(
                line,
                sizeof(line),
                "ERROR:monitor:%d",
                (int)fallbackStatus
            );
            emitLine(line);
            return 3;
        }

        if (parentPid > 0) {
            [NSTimer scheduledTimerWithTimeInterval:2.0
                                            repeats:YES
                                              block:^(NSTimer *timer) {
                if (kill(parentPid, 0) != 0) {
                    [NSApp terminate:nil];
                }
            }];
        }

        [NSApp run];
    }
    return 0;
}
