#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>

@interface LisaAppDelegate : NSObject
    <NSApplicationDelegate, WKScriptMessageHandler, WKNavigationDelegate>
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) WKWebView *webView;
@end

@implementation LisaAppDelegate

- (void)writeLog:(NSString *)message {
    NSString *path = [NSHomeDirectory()
        stringByAppendingPathComponent:@"Desktop/Lisa输出/语音对话/webview.log"];
    NSString *line = [NSString stringWithFormat:@"%@ %@\n",
        [NSDate date], message];
    NSFileManager *manager = NSFileManager.defaultManager;
    if (![manager fileExistsAtPath:path]) {
        [line writeToFile:path
               atomically:YES
                 encoding:NSUTF8StringEncoding
                    error:nil];
        return;
    }
    NSFileHandle *handle = [NSFileHandle fileHandleForWritingAtPath:path];
    [handle seekToEndOfFile];
    [handle writeData:[line dataUsingEncoding:NSUTF8StringEncoding]];
    [handle closeFile];
}

- (void)captureSnapshot {
    WKSnapshotConfiguration *configuration =
        [[WKSnapshotConfiguration alloc] init];
    [self.webView takeSnapshotWithConfiguration:configuration
                              completionHandler:^(NSImage *image,
                                                  NSError *error) {
        if (error || !image) {
            [self writeLog:[NSString stringWithFormat:
                @"snapshot failed: %@", error.localizedDescription]];
            return;
        }
        NSData *tiff = image.TIFFRepresentation;
        NSBitmapImageRep *bitmap =
            [NSBitmapImageRep imageRepWithData:tiff];
        NSData *png = [bitmap
            representationUsingType:NSBitmapImageFileTypePNG
                         properties:@{}];
        NSString *path = [NSHomeDirectory()
            stringByAppendingPathComponent:
                @"Desktop/Lisa输出/语音对话/lisa_web_snapshot.png"];
        BOOL saved = [png writeToFile:path atomically:YES];
        [self writeLog:saved ? @"snapshot saved" : @"snapshot save failed"];
    }];
}

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    (void)notification;
    NSRect frame = NSMakeRect(0, 0, 920, 720);
    self.window = [[NSWindow alloc]
        initWithContentRect:frame
                  styleMask:(NSWindowStyleMaskTitled |
                             NSWindowStyleMaskClosable |
                             NSWindowStyleMaskMiniaturizable |
                             NSWindowStyleMaskResizable)
                    backing:NSBackingStoreBuffered
                      defer:NO];
    self.window.title = @"Codex Voice Bridge";
    self.window.minSize = NSMakeSize(680, 540);
    self.window.backgroundColor = [NSColor whiteColor];
    self.window.appearance =
        [NSAppearance appearanceNamed:NSAppearanceNameAqua];
    self.window.titlebarAppearsTransparent = YES;
    self.window.titleVisibility = NSWindowTitleHidden;
    if ([self.window respondsToSelector:
            @selector(setTitlebarSeparatorStyle:)]) {
        self.window.titlebarSeparatorStyle =
            NSTitlebarSeparatorStyleNone;
    }
    [self.window center];

    WKWebViewConfiguration *configuration =
        [[WKWebViewConfiguration alloc] init];
    [configuration.userContentController
        addScriptMessageHandler:self
                           name:@"lisaShell"];
    self.webView = [[WKWebView alloc]
        initWithFrame:self.window.contentView.bounds
        configuration:configuration];
    self.webView.autoresizingMask =
        NSViewWidthSizable | NSViewHeightSizable;
    self.webView.underPageBackgroundColor = [NSColor whiteColor];
    self.webView.navigationDelegate = self;

    [self.window.contentView addSubview:self.webView];
    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];

    NSString *urlString = NSProcessInfo.processInfo.arguments.count > 1
        ? NSProcessInfo.processInfo.arguments[1]
        : @"http://127.0.0.1:8765/";
    NSURL *url = [NSURL URLWithString:urlString];
    if (url) {
        [self writeLog:[NSString stringWithFormat:@"loading %@", urlString]];
        [self.webView loadRequest:[NSURLRequest requestWithURL:url]];
    } else {
        [self writeLog:@"invalid URL"];
    }
}

- (void)webView:(WKWebView *)webView
    didFinishNavigation:(WKNavigation *)navigation {
    (void)webView;
    (void)navigation;
    [self writeLog:@"loaded"];
}

- (void)webView:(WKWebView *)webView
    didFailNavigation:(WKNavigation *)navigation
            withError:(NSError *)error {
    (void)webView;
    (void)navigation;
    [self writeLog:[NSString stringWithFormat:
        @"navigation failed: %@", error.localizedDescription]];
}

- (void)webView:(WKWebView *)webView
    didFailProvisionalNavigation:(WKNavigation *)navigation
                       withError:(NSError *)error {
    (void)webView;
    (void)navigation;
    [self writeLog:[NSString stringWithFormat:
        @"load failed: %@", error.localizedDescription]];
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:
    (NSApplication *)sender {
    (void)sender;
    return YES;
}

- (void)userContentController:(WKUserContentController *)controller
      didReceiveScriptMessage:(WKScriptMessage *)message {
    (void)controller;
    NSString *action = [message.body isKindOfClass:NSString.class]
        ? message.body
        : @"";
    if ([action isEqualToString:@"ready"]) {
        [self writeLog:@"frontend ready"];
        dispatch_after(
            dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.5 * NSEC_PER_SEC)),
            dispatch_get_main_queue(),
            ^{ [self captureSnapshot]; }
        );
    } else if ([action isEqualToString:@"background"]) {
        [self.window miniaturize:nil];
    } else if ([action isEqualToString:@"exit"]) {
        [NSApp terminate:nil];
    }
}

@end

int main(int argc, const char *argv[]) {
    (void)argc;
    (void)argv;
    @autoreleasepool {
        NSApplication *app = NSApplication.sharedApplication;
        [app setActivationPolicy:NSApplicationActivationPolicyRegular];
        app.appearance =
            [NSAppearance appearanceNamed:NSAppearanceNameAqua];

        NSMenu *menuBar = [[NSMenu alloc] init];
        NSMenuItem *appItem = [[NSMenuItem alloc] init];
        [menuBar addItem:appItem];
        [app setMainMenu:menuBar];
        NSMenu *appMenu = [[NSMenu alloc] initWithTitle:@"Codex Voice Bridge"];
        [appMenu addItemWithTitle:@"退出 Codex Voice Bridge"
                          action:@selector(terminate:)
                   keyEquivalent:@"q"];
        appItem.submenu = appMenu;

        LisaAppDelegate *delegate = [[LisaAppDelegate alloc] init];
        app.delegate = delegate;
        [app run];
    }
    return 0;
}
