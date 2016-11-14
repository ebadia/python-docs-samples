#!/usr/bin/python
# Copyright (C) 2016 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Sample that streams audio to the Google Cloud Speech API via GRPC."""

from __future__ import division

import contextlib
import getopt
import re
import signal
import subprocess
import sys
import threading
import urllib
import webbrowser

from google.cloud import credentials
from google.cloud.speech.v1beta1 import cloud_speech_pb2 as cloud_speech
from google.rpc import code_pb2
from googleapiclient.discovery import build
from grpc.beta import implementations
from grpc.framework.interfaces.face import face
import pyttsx
import pyaudio
from six.moves import queue


# Configuration for Custom Search Engine (CSE) API calls.
CSE_KEY = 'YOUR_API_KEY' # Replace with your developer key
CSE_ID = '00000000012345:aaa7bbb_cc' # Replace with your CSE ID
USE_OSX_SAY = True # Issues on OSX w/ pyttsx; if true, uses 'Say' alternate
OPEN_IN_BROWSER = False # If true, only open the result in a browser

# Audio recording parameters
RATE = 16000
CHUNK = int(RATE / 10)  # 100ms

# The Speech API has a streaming limit of 60 seconds of audio*, so keep the
# connection alive for that long, plus some more to give the API time to figure
# out the transcription.
# * https://g.co/cloud/speech/limits#content
DEADLINE_SECS = 60 * 3 + 6
SPEECH_SCOPE = 'https://www.googleapis.com/auth/cloud-platform'
CSE_SCOPE = 'https://www.googleapis.com/auth/customsearch'

# Global for keeping around the Google search results.
_results = {}
_tts_engine = pyttsx.init()
_index = 0

def make_channel(host, port):
    """Creates an SSL channel with auth credentials from the environment."""
    # In order to make an https call, use an ssl channel with defaults
    ssl_channel = implementations.ssl_channel_credentials(None, None, None)

    # Grab application default credentials from the environment
    creds = credentials.get_credentials().create_scoped([SPEECH_SCOPE])
    # Add a plugin to inject the creds into the header
    auth_header = (
        'Authorization',
        'Bearer ' + creds.get_access_token().access_token)
    auth_plugin = implementations.metadata_call_credentials(
        lambda _, cb: cb([auth_header], None),
        name='google_creds')

    # compose the two together for both ssl and google auth
    composite_channel = implementations.composite_channel_credentials(
        ssl_channel, auth_plugin)

    return implementations.secure_channel(host, port, composite_channel)


def _audio_data_generator(buff):
    """A generator that yields all available data in the given buffer.

    Args:
        buff - a Queue object, where each element is a chunk of data.
    Yields:
        A chunk of data that is the aggregate of all chunks of data in `buff`.
        The function will block until at least one data chunk is available.
    """
    stop = False
    while not stop:
        # Use a blocking get() to ensure there's at least one chunk of data
        chunk = buff.get()
        data = [chunk]

        # Now consume whatever other data's still buffered.
        while True:
            try:
                data.append(buff.get(block=False))
            except queue.Empty:
                break

        # If `_fill_buffer` adds `None` to the buffer, the audio stream is
        # closed. Yield the final bit of the buffer and exit the loop.
        if None in data:
            stop = True
            data.remove(None)
        yield b''.join(data)


def _fill_buffer(audio_stream, buff, chunk, stoprequest):
    """Continuously collect data from the audio stream, into the buffer."""
    try:
        while not stoprequest.is_set():
            buff.put(audio_stream.read(chunk))
    except IOError:
        pass
    finally:
        # Add `None` to the buff, indicating that a stop request is made.
        # This will signal `_audio_data_generator` to exit.
        buff.put(None)


# [START audio_stream]
@contextlib.contextmanager
def record_audio(rate, chunk, stoprequest):
    """Opens a recording stream in a context manager."""
    audio_interface = pyaudio.PyAudio()
    audio_stream = audio_interface.open(
        format=pyaudio.paInt16,
        # The API currently only supports 1-channel (mono) audio
        # https://goo.gl/z757pE
        channels=1, rate=rate,
        input=True, frames_per_buffer=chunk,
    )

    # Create a thread-safe buffer of audio data
    buff = queue.Queue()

    # Spin up a separate thread to buffer audio data from the microphone
    # This is necessary so that the input device's buffer doesn't overflow
    # while the calling thread makes network requests, etc.
    fill_buffer_thread = threading.Thread(
        target=_fill_buffer, args=(audio_stream, buff, chunk, stoprequest))
    fill_buffer_thread.start()

    yield _audio_data_generator(buff)

    fill_buffer_thread.join()
    audio_stream.close()
    audio_interface.terminate()
# [END audio_stream]


def request_stream(data_stream, rate, interim_results=True):
    """Yields `StreamingRecognizeRequest`s constructed from a recording audio
    stream.

    Args:
        data_stream: A generator that yields raw audio data to send.
        rate: The sampling rate in hertz.
        interim_results: Whether to return intermediate results, before the
            transcription is finalized.
    """
    # The initial request must contain metadata about the stream, so the
    # server knows how to interpret it.
    recognition_config = cloud_speech.RecognitionConfig(
        # There are a bunch of config options you can specify. See
        # https://goo.gl/KPZn97 for the full list.
        encoding='LINEAR16',  # raw 16-bit signed LE samples
        sample_rate=rate,  # the rate in hertz
        # See http://g.co/cloud/speech/docs/languages
        # for a list of supported languages.
        language_code='en-US',  # a BCP-47 language tag
    )
    streaming_config = cloud_speech.StreamingRecognitionConfig(
        interim_results=interim_results,
        config=recognition_config,
    )

    yield cloud_speech.StreamingRecognizeRequest(
        streaming_config=streaming_config)

    for data in data_stream:
        # Subsequent requests can all just have the content
        yield cloud_speech.StreamingRecognizeRequest(audio_content=data)


def listen_search_loop(recognize_stream, stoprequest):
    global _index
    global _results
    last_search = ''
    for resp in recognize_stream:
        if resp.error.code != code_pb2.OK:
            raise RuntimeError('Server error: ' + resp.error.message)

        if not resp.results:
            continue
        update_search = True
        say_update = False

        # Exit recognition if any of the transcribed phrases could be
        # one of our keywords.
        if any(re.search(r'\b(exit|quit)\b', alt.transcript, re.I)
               for result in resp.results
               for alt in result.alternatives):
            update_search = False
            print('Exiting..')
            stoprequest.set()
            break

        # Say "Next" to reach the next result.
        if any(re.search(r'\b(next)\b', alt.transcript, re.I)
               for result in resp.results
               for alt in result.alternatives):
            print('>>> Next result')
            update_search = False
            say_update = True
            _index = _index + 1

        # Retrieve search results using speech result
        if update_search and resp.results:
            first_result = resp.results[0].alternatives[0]
            if first_result.transcript != last_search:
                last_search = first_result.transcript
                google_search(last_search, num=10)
                if len(first_result.transcript) > 0:
                    say_update = True
                    _index = 0

        if say_update and _index < len(_results) and not OPEN_IN_BROWSER:
            print('Saying: ' + _results[_index]['snippet'])
            say_result(_index);


def say_result(index):
    """Uses the pyttsx engine to speak the snippet from search to the user."""
    global _results
    if _results[index]['snippet']:
      if USE_OSX_SAY:
          subprocess.call(['say', _results[index]['snippet']])
      else:
          _tts_engine.say(_results[index]['snippet'])
          _tts_engine.runAndWait()

def google_search(search_term, **kwargs):
    """Uses the custom search to query for the user's utterance."""
    global _results
    print 'Searching for :' + search_term

    if OPEN_IN_BROWSER:
      webbrowser.open('https://google.com/#q=' + urllib.quote(search_term))
    else:
      service = build("customsearch", "v1", developerKey=CSE_KEY)
      res = service.cse().list(q=search_term, cx=CSE_ID, **kwargs).execute()
      _results = res['items']

def main(argv):
    global OPEN_IN_BROWSER
    for arg in argv:
        if arg == '--usebrowser':
            print 'Opening results in your default browser.'
            OPEN_IN_BROWSER=True

    if CSE_KEY == 'YOUR_API_KEY' or CSE_ID == '00000000012345:aaa7bbb_cc':
        print 'Sample not configured, opening in default browser.'
        OPEN_IN_BROWSER=True

    with cloud_speech.beta_create_Speech_stub(
            make_channel('speech.googleapis.com', 443)) as service:

        # stoprequest is event object which is set in `listen_search_loop`
        # to indicate that the trancsription should be stopped.
        #
        # The `_fill_buffer` thread checks this object, and closes
        # the `audio_stream` once it's set.
        stoprequest = threading.Event()

        # For streaming audio from the microphone, there are three threads.
        # First, a thread that collects audio data as it comes in
        with record_audio(RATE, CHUNK, stoprequest) as buffered_audio_data:
            # Second, a thread that sends requests with that data
            requests = request_stream(buffered_audio_data, RATE)
            # Third, a thread that listens for transcription responses
            recognize_stream = service.StreamingRecognize(
                requests, DEADLINE_SECS)

            # Exit things cleanly on interrupt
            signal.signal(signal.SIGINT, lambda *_: recognize_stream.cancel())

            # Now, put the transcription responses to use.
            try:
                listen_search_loop(recognize_stream, stoprequest)
                recognize_stream.cancel()
            except face.CancellationError:
                # This happens because of the interrupt handler
                pass


if __name__ == '__main__':
    main(sys.argv[1:])