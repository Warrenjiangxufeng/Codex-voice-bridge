#import <AVFoundation/AVFoundation.h>
#import <Foundation/Foundation.h>

@interface TTSSpeaker : NSObject <AVSpeechSynthesizerDelegate>
@property(nonatomic, assign) BOOL finished;
@property(nonatomic, assign) NSInteger remaining;
@end

@implementation TTSSpeaker
- (void)speechSynthesizer:(AVSpeechSynthesizer *)synthesizer
    didFinishSpeechUtterance:(AVSpeechUtterance *)utterance {
    self.remaining -= 1;
    if (self.remaining <= 0) {
        self.finished = YES;
    }
}

- (void)speechSynthesizer:(AVSpeechSynthesizer *)synthesizer
    didCancelSpeechUtterance:(AVSpeechUtterance *)utterance {
    self.finished = YES;
}
@end

static void print_voices(void) {
    for (AVSpeechSynthesisVoice *voice in [AVSpeechSynthesisVoice speechVoices]) {
        if (![voice.language hasPrefix:@"zh"]) {
            continue;
        }
        printf("%s\t%s\t%s\t%ld\n",
               voice.identifier.UTF8String,
               voice.name.UTF8String,
               voice.language.UTF8String,
               (long)voice.quality);
    }
}

static AVSpeechSynthesisVoice *find_voice(NSString *identifier) {
    AVSpeechSynthesisVoice *voice =
        [AVSpeechSynthesisVoice voiceWithIdentifier:identifier];
    if (voice) {
        return voice;
    }

    NSArray<NSString *> *fallbacks = @[
        @"com.apple.speech.synthesis.voice.custom.siri.li-mu.premium",
        @"com.apple.speech.synthesis.voice.custom.siri.linfei.premium",
        @"com.apple.speech.synthesis.voice.ting-ting.premium",
        @"com.apple.speech.synthesis.voice.custom.siri.yu-shu.premium",
    ];
    for (NSString *candidate in fallbacks) {
        voice = [AVSpeechSynthesisVoice voiceWithIdentifier:candidate];
        if (voice) {
            return voice;
        }
    }
    return [AVSpeechSynthesisVoice voiceWithLanguage:@"zh-CN"];
}

static BOOL is_sentence_end(unichar character) {
    return character == L'。' || character == L'！' || character == L'？' ||
           character == L'!' || character == L'?' || character == L'；' ||
           character == L';' || character == L'…';
}

static BOOL is_clause_end(unichar character) {
    return character == L'，' || character == L',' || character == L'、' ||
           character == L'：' || character == L':';
}

static BOOL is_closing_mark(unichar character) {
    return character == L'”' || character == L'’' || character == L')' ||
           character == L'）' || character == L'"';
}

static NSArray<NSString *> *split_text(NSString *text) {
    NSMutableArray<NSString *> *sentences = [NSMutableArray array];
    NSMutableString *current = [NSMutableString string];

    for (NSUInteger index = 0; index < text.length; index++) {
        unichar character = [text characterAtIndex:index];
        [current appendFormat:@"%C", character];
        if (!is_sentence_end(character)) {
            continue;
        }
        while (index + 1 < text.length &&
               is_closing_mark([text characterAtIndex:index + 1])) {
            [current appendFormat:@"%C", [text characterAtIndex:++index]];
        }
        NSString *sentence = [current
            stringByTrimmingCharactersInSet:
                [NSCharacterSet whitespaceAndNewlineCharacterSet]];
        if (sentence.length > 0) {
            [sentences addObject:sentence];
        }
        [current setString:@""];
    }

    NSString *tail = [current
        stringByTrimmingCharactersInSet:
            [NSCharacterSet whitespaceAndNewlineCharacterSet]];
    if (tail.length > 0) {
        [sentences addObject:tail];
    }

    NSMutableArray<NSString *> *chunks = [NSMutableArray array];
    for (NSString *sentence in sentences) {
        if (sentence.length <= 28) {
            [chunks addObject:sentence];
            continue;
        }
        NSMutableString *clause = [NSMutableString string];
        for (NSUInteger index = 0; index < sentence.length; index++) {
            unichar character = [sentence characterAtIndex:index];
            [clause appendFormat:@"%C", character];
            if (is_clause_end(character) && clause.length >= 8) {
                [chunks addObject:[clause copy]];
                [clause setString:@""];
            }
        }
        if (clause.length > 0) {
            [chunks addObject:clause];
        }
    }
    return chunks;
}

static unichar last_effective_character(NSString *text) {
    for (NSInteger index = (NSInteger)text.length - 1; index >= 0; index--) {
        unichar character = [text characterAtIndex:(NSUInteger)index];
        if (![[NSCharacterSet whitespaceAndNewlineCharacterSet]
                characterIsMember:character]) {
            return character;
        }
    }
    return 0;
}

static void configure_utterance(
    AVSpeechUtterance *utterance,
    NSString *text,
    NSUInteger index,
    NSString *style
) {
    float rate = 0.47;
    float pitch = 0.99;
    float pause = 0.12;

    if ([style isEqualToString:@"calm"]) {
        rate = 0.43;
        pitch = 0.97;
    } else if ([style isEqualToString:@"lively"]) {
        rate = 0.52;
        pitch = 1.03;
    }

    unichar last = last_effective_character(text);
    if (last == L'？' || last == L'?') {
        rate -= 0.01;
        pitch += 0.035;
        pause = 0.20;
    } else if (last == L'！' || last == L'!') {
        rate += 0.025;
        pitch += 0.05;
        pause = 0.16;
    } else if (last == L'…') {
        rate -= 0.055;
        pause = 0.32;
    } else if (is_clause_end(last)) {
        pause = 0.075;
    }

    if (text.length <= 5) {
        rate += 0.02;
    } else if (text.length > 32) {
        rate -= 0.02;
    }

    // A very small alternating lift avoids a flat, one-note delivery.
    pitch += (index % 2 == 0) ? 0.004 : -0.004;

    utterance.rate = MAX(0.35f, MIN(0.58f, rate));
    utterance.pitchMultiplier = MAX(0.90f, MIN(1.12f, pitch));
    utterance.volume = 1.0;
    utterance.preUtteranceDelay = 0.02;
    utterance.postUtteranceDelay = pause;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSString *voiceIdentifier = nil;
        NSString *style = @"warm";
        NSMutableArray<NSString *> *textParts = [NSMutableArray array];

        for (int index = 1; index < argc; index++) {
            NSString *argument =
                [NSString stringWithUTF8String:argv[index]];
            if ([argument isEqualToString:@"--list"]) {
                print_voices();
                return 0;
            }
            if ([argument isEqualToString:@"--voice"] && index + 1 < argc) {
                voiceIdentifier =
                    [NSString stringWithUTF8String:argv[++index]];
                continue;
            }
            if ([argument isEqualToString:@"--style"] && index + 1 < argc) {
                style = [NSString stringWithUTF8String:argv[++index]];
                continue;
            }
            [textParts addObject:argument];
        }

        NSString *text =
            [[textParts componentsJoinedByString:@" "]
                stringByTrimmingCharactersInSet:
                    [NSCharacterSet whitespaceAndNewlineCharacterSet]];
        if (text.length == 0) {
            fprintf(stderr, "用法: lisa_tts [--list] [--voice ID] <文字>\n");
            return 2;
        }

        AVSpeechSynthesisVoice *voice =
            find_voice(voiceIdentifier ?: @"");
        if (!voice) {
            fprintf(stderr, "没有可用的中文语音\n");
            return 3;
        }

        NSArray<NSString *> *chunks = split_text(text);
        if (chunks.count == 0) {
            fprintf(stderr, "没有可朗读的文本\n");
            return 2;
        }

        TTSSpeaker *speaker = [TTSSpeaker new];
        speaker.remaining = (NSInteger)chunks.count;
        AVSpeechSynthesizer *synthesizer = [AVSpeechSynthesizer new];
        synthesizer.delegate = speaker;

        NSUInteger index = 0;
        for (NSString *chunk in chunks) {
            AVSpeechUtterance *utterance =
                [AVSpeechUtterance speechUtteranceWithString:chunk];
            utterance.voice = voice;
            configure_utterance(utterance, chunk, index++, style);
            [synthesizer speakUtterance:utterance];
        }

        NSDate *deadline =
            [NSDate dateWithTimeIntervalSinceNow:
                MAX(30.0, text.length / 3.0 + chunks.count * 0.5)];
        while (!speaker.finished && deadline.timeIntervalSinceNow > 0) {
            [[NSRunLoop currentRunLoop]
                runMode:NSDefaultRunLoopMode
                beforeDate:[NSDate dateWithTimeIntervalSinceNow:0.1]];
        }
        if (!speaker.finished) {
            [synthesizer stopSpeakingAtBoundary:AVSpeechBoundaryImmediate];
            fprintf(stderr, "朗读超时\n");
            return 4;
        }
    }
    return 0;
}
