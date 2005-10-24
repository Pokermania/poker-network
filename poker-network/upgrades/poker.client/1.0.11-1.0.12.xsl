<?xml version="1.0" encoding="ISO-8859-1"?>

<xsl:stylesheet version="1.0"
                xmlns:xsl="http://www.w3.org/1999/XSL/Transform">

 <xsl:preserve-space elements="*" />
 <xsl:output method="xml" indent="yes"
	     encoding="ISO-8859-1"
 />

 <!-- Send ping packet every 15 seconds by default -->
 <xsl:template match="/settings/@verbose">
   <xsl:attribute name="ping">15</xsl:attribute>
   <xsl:attribute name="verbose"><xsl:value-of select="." /></xsl:attribute>
 </xsl:template>
 
 <!-- copy the rest verbatim -->
 <xsl:template match="@*|node()">
  <xsl:copy>
   <xsl:apply-templates select="@*|node()"/>
  </xsl:copy>
 </xsl:template>

</xsl:stylesheet>
