""" extracted/adapted from txtenna-python project
"""
import cmd # for the command line application
import os
import traceback
import logging
import requests
import json
from threading import Thread
from time import sleep
import random
import string
import binascii
from segment_storage import SegmentStorage
from txtenna_segment import TxTennaSegment
from io import BytesIO
## import httplib
import struct
import zlib

# Import support for bitcoind RPC interface
import bitcoin
import bitcoin.rpc
from bitcoin.core import x, lx, b2x, b2lx, CMutableTxOut, CMutableTransaction
from bitcoin.wallet import P2WPKHBitcoinAddress

BYTE_STRING_CBOR_TAG = 24
BITCOIN_NETWORK_CBOR_TAG = 27
SEGMENT_NUMBER_CBOR_TAG = 28
SEGMENT_COUNT_CBOR_TAG = 29
SHORT_TXID_CBOR_TAG = 30
TXID_CBOR_TAG = 31

bitcoin.SelectParams('mainnet')

class TxTenna(cmd.Cmd):
    def __init__(self, local_gid, local_bitcoind, send_dir, receive_dir, pipe):

        # store txtenna segments
        self.segment_storage = SegmentStorage()

        # the GID of this node
        self.local_gid = local_gid

        ## use local bitcoind to confirm transactions if 'local' is true
        self.local_bitcoind = local_bitcoind

        ## broadcast message data from files in this directory, eg. created by the blocksat
        self.send_dir = send_dir
        if (send_dir is not None):
            self.do_broadcast_messages(send_dir)

        self.pipe_file = pipe
        self.receive_dir = receive_dir

    def do_send_private(self, args) :
        print("do_send_private undefined in TxTenna class.")

    def do_send_broadcast(self, args) :
        print("do_send_broadcast undefined in TxTenna class.")

    def do_rpc_getrawtransaction(self, tx_id) :
        """
        Call local Bitcoin RPC method 'getrawtransaction'

        Usage: rpc_sendtoaddress TX_ID
        """
        try :
            proxy = bitcoin.rpc.Proxy()
            r = proxy.getrawtransaction(lx(tx_id), True)
            print(str(r))
        except:
            traceback.print_exc()

    def confirm_bitcoin_tx_local(self, hash, sender_gid):
        """ 
        Confirm bitcoin transaction using local bitcoind instance

        Usage: confirm_bitcoin_tx tx_id gid
        """ 

        ## send transaction to local bitcond
        segments = self.segment_storage.get_by_transaction_id(hash)
        raw_tx = self.segment_storage.get_raw_tx(segments)

        ## pass hex string converted to bytes
        try :
            proxy1 = bitcoin.rpc.Proxy()
            raw_tx_bytes = x(raw_tx)
            tx = CMutableTransaction.stream_deserialize(BytesIO(raw_tx_bytes))
            r1 = proxy1.sendrawtransaction(tx)
        except :
            print("Invalid Transaction! Could not send to network.")
            return

        ## try for 30 minutes to confirm the transaction
        for n in range(0, 30) :
            try :
                proxy2 = bitcoin.rpc.Proxy()
                r2 = proxy2.getrawtransaction(r1, True)

                ## send zero-conf message back to tx sender
                confirmations = r2.get('confirmations', 0)
                rObj = TxTennaSegment('', '', tx_hash=hash, block=confirmations)
                # arg = str(sender_gid) + ' ' + rObj.serialize_to_json()
                arg = str(sender_gid) + " Transaction " + hash + " added to the mempool."
                self.do_send_private(arg)

                print("\nSent to GID: " + str(sender_gid) + ": Transaction " + hash + " added to the mempool.")
                break      
            except IndexError:
                ## tx_id not yet in the global mempool, sleep for a minute and then try again
                sleep(60)
                continue      
            
            ## wait for atleast one confirmation
            for m in range(0, 30):
                sleep(60) # sleep for a minute
                try :
                    proxy3= bitcoin.rpc.Proxy()
                    r3 = proxy3.getrawtransaction(r1, True)
                    confirmations = r3.get('confirmations', 0)
                    ## keep waiting until 1 or more confirmations
                    if confirmations > 0:
                        break
                except :
                    ## unknown RPC error, but keep trying
                    traceback.print_exc()

            if confirmations > 0 :
                ## send confirmations message back to tx sender if confirmations > 0
                rObj = TxTennaSegment('', '', tx_hash=hash, block=confirmations)
                ## arg = str(sender_gid) + ' ' + rObj.serialize_to_json()
                arg = str(sender_gid) + " Transaction " + hash + " confirmed in " + str(confirmations) + " blocks."
                self.do_send_private(arg)
                print("\nSent to GID: " + str(sender_gid) + ", Transaction " + hash + " confirmed in " + str(confirmations) + " blocks.")
            else :
                arg = str(sender_gid) + " Transaction " + hash + " not confirmed after 30 minutes."
                self.do_send_private(arg)
                print("\CTransaction from GID: " + str(sender_gid) + ", Transaction " + hash + " not confirmed after 30 minutes.")

    def confirm_bitcoin_tx_online(self, hash, sender_gid, network):
        """ confirm bitcoin transaction using default online Samourai API instance

        Usage: confirm_bitcoin_tx tx_id gid network
        """

        if  network == 't' :
            url = "https://api.samourai.io/test/v2/tx/" + hash ## default testnet txtenna-server
        else :
            url = "https://api.samourai.io/v2/tx/" + hash ## default txtenna-server
        
        try:
            r = requests.get(url)
            ## print(r.text)

            while r.status_code != 200:
                sleep(60) # sleep for a minute
                r = requests.get(url)

            ## send zero-conf message back to tx sender
            rObj = TxTennaSegment('', '', tx_hash=hash, block=0)
            # arg = str(sender_gid) + ' ' + rObj.serialize_to_json()
            arg = str(sender_gid) + " Transaction " + hash + " added to the mempool."
            self.do_send_private(arg)    

            print("\nSent to GID: " + str(sender_gid) + ": Transaction " + hash + " added to the mempool.")            

            r_text = "".join(r.text.split()) # remove whitespace
            obj = json.loads(r_text)
            while not 'block' in obj.keys():
                sleep(60) # sleep for a minute
                r = requests.get(url)
                r_text = "".join(r.text.split())
                obj = json.loads(r_text)

            ## send block height message back to tx sender
            blockheight = obj['block']['height']
            rObj = TxTennaSegment('', '', tx_hash=hash, block=blockheight)
            # arg = str(sender_gid) + ' ' + rObj.serialize_to_json()
            # TODO: merkle proof for transaction
            arg = str(sender_gid) + " Transaction " + hash + " confirmed in block " + str(blockheight) + "."
            self.do_send_private(arg)

            print("\nSent to GID: " + str(sender_gid) + ": Transaction " + hash + " confirmed in block " + str(blockheight) + ".")

        except:
            traceback.print_exc()

    def create_output_data_struct(self, data):
        """Create the output data structure generated by the blocksat receiver

        The "Protocol Sink" block of the blocksat-rx application places the incoming
        API data into output structures. This function creates the exact same
        structure that the blocksat-rx application would.

        Args:
            data : Sequence of bytes to be placed in the output structure

        Returns:
            Output data structure as sequence of bytes

        """

        # Header of the output data structure that the Blockstream Satellite Receiver
        # generates prior to writing user data into the API named pipe
        OUT_DATA_HEADER_FORMAT     = '64sQ'
        OUT_DATA_DELIMITER         = 'vyqzbefrsnzqahgdkrsidzigxvrppato' + \
                                '\xe0\xe0$\x1a\xe4["\xb5Z\x0bv\x17\xa7\xa7\x9d' + \
                                '\xa5\xd6\x00W}M\xa6TO\xda7\xfaeu:\xac\xdc'

        # Struct is composed of a delimiter and the message length
        out_data_header = struct.pack(OUT_DATA_HEADER_FORMAT,
                                    OUT_DATA_DELIMITER,
                                    len(data))

        # Final output data structure
        out_data = out_data_header + data

        return out_data            
            
    def receive_message_from_gateway(self, filename):
        """ 
        Receive message data from a mesh gateway node

        Usage: receive_message_from_gateway filename
        """ 

        ## send transaction to local blocksat reader pipe
        segments = self.segment_storage.get_by_transaction_id(filename)
        raw_data = self.segment_storage.get_raw_tx(segments).encode("utf-8")

        decoded_data = zlib.decompress(raw_data.decode('base64'))

        deliminted_data = self.create_output_data_struct(decoded_data)

        ## send the data to the blocksat pipe
        try :
            print("Message Data received for [" + filename + "] ( " + str(len(decoded_data)) + " bytes ) :\n" + str(decoded_data) + "\n")
        except UnicodeDecodeError :
            print("Binary Data received for [" + filename + "] ( " + str(len(decoded_data)) + " bytes )\n")
        
        if not self.pipe_file is None and os.path.exists(self.pipe_file) is True :
            # Open pipe and write raw data to it
            pipe_f = os.open(self.pipe_file, os.O_RDWR)
            os.write(pipe_f, deliminted_data)
        elif not self.receive_dir is None and os.path.exists(self.receive_dir) is True :
            # Create file
            dump_f = os.open(os.path.join(self.receive_dir, filename), os.O_CREAT | os.O_RDWR)
            os.write(dump_f, decoded_data)
        else :
            print("ERROR: Could not save data. No pipe found at [" + self.pipe_file + "] and no receive directory found at [" + self.receive_dir +"]\n")

    def cbor_to_txtenna_json(self, protocol_msg):

        data = protocol_msg[BYTE_STRING_CBOR_TAG]
        if SEGMENT_NUMBER_CBOR_TAG in protocol_msg:
            segment = protocol_msg[SEGMENT_NUMBER_CBOR_TAG]
        else:
            segment = 0
        
        if (segment == 0):
            # only first segment contains network (optional), transaction hash and segment count
            if BITCOIN_NETWORK_CBOR_TAG in protocol_msg:
                network = chr(protocol_msg[BITCOIN_NETWORK_CBOR_TAG])
            else:
                network = 'm'
            short_txid = protocol_msg[SHORT_TXID_CBOR_TAG]
            txid = protocol_msg[TXID_CBOR_TAG]
            count = protocol_msg[SEGMENT_COUNT_CBOR_TAG]
            print("short_txid=" + short_txid.hex() + ", txid=" + txid.hex()+ ", network=" + str(network) + ", segment=" + str(segment) + ", count=" + str(count))
            json_out = json.dumps({"i": short_txid.hex(), "h": txid.hex(), "t": data.hex(), "n": network, "c": segment, "s": count})
        else:
            short_txid = protocol_msg[SHORT_TXID_CBOR_TAG]
            print("short_txid=" + short_txid.hex() + ", segment=" + str(segment))
            json_out = json.dumps({"i": short_txid.hex(), "t": data.hex(), "c": segment})

        print(" data: " + data.hex())

        return json_out

    def handle_cbor_message(self, sender_gid, protocol_msg):

        # TODO: 1a) concatonate segments and send as new transaction to block explorer 
        # TODO: 1b) send acknowledgement back to Signal Mesh as a text message
        # TODO: 2a) monitor for transaction to be confirmed in a block
        # TODO: 2b) send blockchain confirmation back to Signal Mesh as a text message

        txtenna_json = self.cbor_to_txtenna_json(protocol_msg)
        segment = TxTennaSegment.deserialize_from_json(txtenna_json)
        self.segment_storage.put(segment)
        network = self.segment_storage.get_network(segment.payload_id)

        ## process incoming transaction confirmation from another server
        if (segment.block != None):
            if (segment.block > 0):
                print("\nTransaction " + segment.payload_id + " confirmed in block " + str(segment.block))
            elif (segment.block is 0):
                print("\nTransaction " + segment.payload_id + " added to the the mem pool")
        elif (network is 'd'):
            ## process message data
            if (self.segment_storage.is_complete(segment.payload_id)):
                filename = self.segment_storage.get_transaction_id(segment.payload_id)
                t = Thread(target=self.receive_message_from_gateway, args=(filename,))
                t.start()
        else:
            ## process incoming tx segment
            if not self.local_bitcoind :
                headers = {u'content-type': u'application/json'}
                url = "https://api.samouraiwallet.com/v2/txtenna/segments" ## default txtenna-server
                r = requests.post(url, headers= headers, data=txtenna_json)
                print(r.text)

            if (self.segment_storage.is_complete(segment.payload_id)):
                # sender_gid = message.sender.gid_val
                tx_id = self.segment_storage.get_transaction_id(segment.payload_id)

                ## check for confirmed transaction in a new thread
                if (self.local_bitcoind) :
                    t = Thread(target=self.confirm_bitcoin_tx_local, args=(tx_id, sender_gid))
                else :
                    t = Thread(target=self.confirm_bitcoin_tx_online, args=(tx_id, sender_gid, network))
                t.start()

    def do_mesh_broadcast_rawtx(self, rem):
        """ 
        Broadcast the raw hex of a Bitcoin transaction and its transaction ID over mainnet or testnet. 
        A local copy of txtenna-server must be configured to support the selected network.

        Usage: mesh_broadcast_tx RAW_HEX TX_ID NETWORK(m|t)

        eg. txTenna> mesh_broadcast_rawtx 01000000000101bf6c3ed233e8700b42c1369993c2078780015bab7067b9751b7f49f799efbffd0000000017160014f25dbf0eab0ba7e3482287ebb41a7f6d361de6efffffffff02204e00000000000017a91439cdb4242013e108337df383b1bf063561eb582687abb93b000000000017a9148b963056eedd4a02c91747ea667fc34548cab0848702483045022100e92ce9b5c91dbf1c976d10b2c5ed70d140318f3bf2123091d9071ada27a4a543022030c289d43298ca4ca9d52a4c85f95786c5e27de5881366d9154f6fe13a717f3701210204b40eff96588033722f487a52d39a345dc91413281b31909a4018efb330ba2600000000 94406beb94761fa728a2cde836ca636ecd3c51cbc0febc87a968cb8522ce7cc1 m
        """

        ## TODO: test Z85 binary encoding and add as an option
        (strHexTx, strHexTxHash, network) = rem.split(" ")
        # local_gid = self.api_thread.gid.gid_val
        segments = TxTennaSegment.tx_to_segments(self.local_gid, strHexTx, strHexTxHash, str(self.messageIdx), network, False)
        for seg in segments :
            self.do_send_broadcast(seg.serialize_to_json())
            sleep(10)
        self.messageIdx = (self.messageIdx+1) % 9999

    def do_rpc_getbalance(self, rem) :
        """
        Call local Bitcoin RPC method 'getbalance'

        Usage: rpc_getbalance
        """
        try :
            proxy = bitcoin.rpc.Proxy()
            balance = proxy.getbalance()
            print("getbalance: " + str(balance))
        except Exception: # pylint: disable=broad-except
            traceback.print_exc()     

    def do_rpc_sendrawtransaction(self, hex) :
        """
        Call local Bitcoin RPC method 'sendrawtransaction'

        Usage: rpc_sendrawtransaction RAW_TX_HEX
        """
        try :
            proxy = bitcoin.rpc.Proxy()
            r = proxy.sendrawtransaction(hex)
            print("sendrawtransaction: " + str(r))
        except Exception: # pylint: disable=broad-except
            traceback.print_exc()

    def do_rpc_sendtoaddress(self, rem) :
        """
        Call local Bitcoin RPC method 'sendtoaddress'

        Usage: rpc_sendtoaddress ADDRESS SATS
        """
        try:
            proxy = bitcoin.rpc.Proxy()
            (addr, amount) = rem.split()
            r = proxy.sendtoaddress(addr, amount)
            print("sendtoaddress, transaction id: " + str(r["hex"]))
        except Exception: # pylint: disable=broad-except
            traceback.print_exc() 

    def do_mesh_sendtoaddress(self, rem) :
        """ 
        Create a signed transaction and broadcast it over the connected mesh device. The transaction 
        spends some amount of satoshis to the specified address from the local bitcoind wallet and selected network. 

        Usage: mesh_sendtoaddress BECH32 ADDRESS SATS NETWORK(m|t)

        eg. txTenna> mesh_sendtoaddress 2N4BtwKZBU3kXkWT7ZBEcQLQ451AuDWiau2 13371337 t
        """
        try:

            proxy = bitcoin.rpc.Proxy()
            (addr, sats, network) = rem.split()

            # Create the txout. This time we create the scriptPubKey from a Bitcoin
            # address.
            p2wpkh_addr = P2WPKHBitcoinAddress(addr)
            txout = CMutableTxOut(sats, p2wpkh_addr.to_scriptPubKey())

            # Create the unsigned transaction.
            unfunded_transaction = CMutableTransaction([], [txout])
            funded_transaction = proxy.fundrawtransaction(unfunded_transaction)
            signed_transaction = proxy.signrawtransaction(funded_transaction["tx"])
            txhex = b2x(signed_transaction["tx"].serialize())
            txid = b2lx(signed_transaction["tx"].GetTxid())
            print("sendtoaddress_mesh (tx, txid, network): " + txhex + ", " + txid, ", " + network)

            # broadcast over mesh
            self.do_mesh_broadcast_rawtx( txhex + " " + txid + " " + network)

        except Exception: # pylint: disable=broad-except
            traceback.print_exc()

        try :
            # lock UTXOs used to fund the tx if broadcast successful
            vin_outpoints = set()
            for txin in funded_transaction["tx"].vin:
                vin_outpoints.add(txin.prevout)
            ## json_outpoints = [{'txid':b2lx(outpoint.hash), 'vout':outpoint.n}
            ##              for outpoint in vin_outpoints]
            ## print(str(json_outpoints))
            proxy.lockunspent(False, vin_outpoints)
            
        except Exception: # pylint: disable=broad-except
            ## TODO: figure out why this is happening
            print("RPC timeout after calling lockunspent")

    def do_broadcast_messages(self, send_dir) :
        """ 
        Watch a particular directory for files with message data to be broadcast over the mesh network

        Usage: broadcast_messages DIRECTORY

        eg. txTenna> broadcast_messages ./downloads
        """

        if (send_dir is not None):
            #start new thread to watch directory
            self.watch_dir_thread = Thread(target=self.watch_messages, args=(send_dir,))
            self.watch_dir_thread.start()

    def watch_messages(self, send_dir):
        
        before = {}
        while os.path.exists(send_dir):
            sleep (10)
            after = dict ([(f, None) for f in os.listdir (send_dir)])
            new_files = [f for f in after if not f in before]
            if new_files:
                self.broadcast_message_files(send_dir, new_files)
            before = after

    def broadcast_message_files(self, directory, filenames):
        for filename in filenames:
            print("Broadcasting ",directory+"/"+filename)
            f = open(directory+"/"+filename,'r')
            message_data = f.read()
            f.close
            
            ## binary to ascii encoding and strip out newlines
            encoded = zlib.compress(message_data, 9).encode('base64').replace('\n','')
            print("[\n" + encoded.decode() + "\n]")

            # local_gid = self.api_thread.gid.gid_val
            segments = TxTennaSegment.tx_to_segments(self.local_gid, encoded, filename, str(self.messageIdx), "d", False)
            for seg in segments :
                self.do_send_broadcast(seg.serialize_to_json())
                sleep(10)
            self.messageIdx = (self.messageIdx+1) % 9999